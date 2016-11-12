"""
Like described in the :mod:`jedi.parser.tree` module,
there's a need for an ast like module to represent the states of parsed
modules.

But now there are also structures in Python that need a little bit more than
that. An ``Instance`` for example is only a ``Class`` before it is
instantiated. This class represents these cases.

So, why is there also a ``Class`` class here? Well, there are decorators and
they change classes in Python 3.

Representation modules also define "magic methods". Those methods look like
``py__foo__`` and are typically mappable to the Python equivalents ``__call__``
and others. Here's a list:

====================================== ========================================
**Method**                             **Description**
-------------------------------------- ----------------------------------------
py__call__(params: Array)              On callable objects, returns types.
py__bool__()                           Returns True/False/None; None means that
                                       there's no certainty.
py__bases__()                          Returns a list of base classes.
py__mro__()                            Returns a list of classes (the mro).
py__iter__()                           Returns a generator of a set of types.
py__class__()                          Returns the class of an instance.
py__getitem__(index: int/str)          Returns a a set of types of the index.
                                       Can raise an IndexError/KeyError.
py__file__()                           Only on modules.
py__package__()                        Only on modules. For the import system.
py__path__()                           Only on modules. For the import system.
====================================== ========================================

__
"""
import os
import pkgutil
import imp
import re

from jedi._compatibility import use_metaclass, unicode, Python3Method, is_py3
from jedi.parser import tree
from jedi import debug
from jedi import common
from jedi.cache import underscore_memoization, cache_star_import
from jedi.evaluate.cache import memoize_default, CachedMetaClass, NO_DEFAULT
from jedi.evaluate import compiled
from jedi.evaluate.compiled import mixed
from jedi.evaluate import recursion
from jedi.evaluate import iterable
from jedi.evaluate import docstrings
from jedi.evaluate import pep0484
from jedi.evaluate import param
from jedi.evaluate import flow_analysis
from jedi.evaluate import imports
from jedi.evaluate.filters import ParserTreeFilter, FunctionExecutionFilter, \
    GlobalNameFilter, DictFilter, ContextName, AbstractNameDefinition
from jedi.evaluate.dynamic import search_params
from jedi.evaluate import context


class Executed(context.TreeContext):
    """
    An instance is also an executable - because __init__ is called
    :param var_args: The param input array, consist of a parser node or a list.
    """
    def __init__(self, evaluator, parent_context, var_args):
        super(Executed, self).__init__(evaluator, parent_context=parent_context)
        self.var_args = var_args

    def is_scope(self):
        return True


class Instance(use_metaclass(CachedMetaClass, Executed)):
    """
    This class is used to evaluate instances.
    """
    def __init__(self, evaluator, parent_context, class_context, var_args, is_generated=False):
        super(Instance, self).__init__(evaluator, parent_context, var_args)
        # Generated instances are classes that are just generated by self
        # (No var_args) used.
        self.is_generated = is_generated
        self._class_context = class_context

        self._init_execution = None
        if class_context.name.string_name in ['list', 'set'] \
                and evaluator.BUILTINS == parent_context.get_root_context():
            # compare the module path with the builtin name.
            self.var_args = iterable.check_array_instances(evaluator, self)
        elif not is_generated:
            # Need to execute the __init__ function, because the dynamic param
            # searching needs it.
            try:
                method = self.get_subscope_by_name('__init__')
            except KeyError:
                pass
            else:
                self._init_execution = evaluator.execute(method, self.var_args)

    def is_class(self):
        return False

    @property
    def py__call__(self):
        def actual(params):
            return self.evaluator.execute(method, params)

        try:
            method = self.get_subscope_by_name('__call__')
        except KeyError:
            # Means the Instance is not callable.
            raise AttributeError

        return actual

    def py__class__(self):
        return self.class_context

    def py__bool__(self):
        # Signalize that we don't know about the bool type.
        return None

    @memoize_default()
    def _get_init_execution(self):
        try:
            func = self.get_subscope_by_name('__init__')
        except KeyError:
            return None
        return FunctionExecutionContext(self.evaluator, self, func, self.var_args)

    def _get_func_self_name(self, func):
        """
        Returns the name of the first param in a class method (which is
        normally self.
        """
        try:
            return str(func.params[0].name)
        except IndexError:
            return None

    def _self_names_dict(self, add_mro=True):
        names = {}
        # This loop adds the names of the self object, copies them and removes
        # the self.
        for sub in self.base.subscopes:
            if isinstance(sub, tree.ClassContext):
                continue
            # Get the self name, if there's one.
            self_name = self._get_func_self_name(sub)
            if self_name is None:
                continue

            if sub.name.value == '__init__' and not self.is_generated:
                # ``__init__`` is special because the params need are injected
                # this way. Therefore an execution is necessary.
                if not sub.get_decorators():
                    # __init__ decorators should generally just be ignored,
                    # because to follow them and their self variables is too
                    # complicated.
                    sub = self._get_init_execution()
            for name_list in sub.names_dict.values():
                for name in name_list:
                    if name.value == self_name and name.get_previous_sibling() is None:
                        trailer = name.get_next_sibling()
                        if tree.is_node(trailer, 'trailer') \
                                and len(trailer.children) == 2 \
                                and trailer.children[0] == '.':
                            name = trailer.children[1]  # After dot.
                            if name.is_definition():
                                arr = names.setdefault(name.value, [])
                                arr.append(get_instance_el(self.evaluator, self, name))
        return names

    def get_subscope_by_name(self, name):
        sub = self._class_context.get_subscope_by_name(name)
        return get_instance_el(self.evaluator, self, sub, True)

    def execute_subscope_by_name(self, name, *args):
        method = self.get_subscope_by_name(name)
        return self.evaluator.execute_evaluated(method, *args)

    def get_descriptor_returns(self, obj):
        """ Throws a KeyError if there's no method. """
        # Arguments in __get__ descriptors are obj, class.
        # `method` is the new parent of the array, don't know if that's good.
        none_obj = compiled.create(self.evaluator, None)
        args = [obj, obj.base] if isinstance(obj, Instance) else [none_obj, obj]
        try:
            return self.execute_subscope_by_name('__get__', *args)
        except KeyError:
            return set([self])

    @memoize_default()
    def names_dicts(self, search_global):
        yield self._self_names_dict()

        for s in self._class_context.py__mro__()[1:]:
            if not isinstance(s, compiled.CompiledObject):
                # Compiled objects don't have `self.` names.
                for inst in self.evaluator.execute(s):
                    yield inst._self_names_dict(add_mro=False)

        for names_dict in self.base.names_dicts(search_global=False, is_instance=True):
            yield LazyInstanceDict(self.evaluator, self, names_dict)

    def get_filters(self, search_global, until_position=None, origin_scope=None):
        for cls in self._class_context.py__mro__():
            if isinstance(cls, compiled.CompiledObject):
                yield SelfNameFilter(self.evaluator, self, cls, origin_scope)
            else:
                yield SelfNameFilter(self.evaluator, self, cls.base, origin_scope)

        for cls in self._class_context.py__mro__():
            if isinstance(cls, compiled.CompiledObject):
                yield CompiledInstanceClassFilter(self.evaluator, self, cls)
            else:
                yield InstanceClassFilter(self.evaluator, self, cls.base, origin_scope)

    def py__getitem__(self, index):
        try:
            method = self.get_subscope_by_name('__getitem__')
        except KeyError:
            debug.warning('No __getitem__, cannot access the array.')
            return set()
        else:
            index_obj = compiled.create(self.evaluator, index)
            return self.evaluator.execute_evaluated(method, index_obj)

    def py__iter__(self):
        try:
            method = self.get_subscope_by_name('__iter__')
        except KeyError:
            debug.warning('No __iter__ on %s.' % self)
            return
        else:
            iters = self.evaluator.execute(method)
            for generator in iters:
                if isinstance(generator, Instance):
                    # `__next__` logic.
                    name = '__next__' if is_py3 else 'next'
                    try:
                        yield generator.execute_subscope_by_name(name)
                    except KeyError:
                        debug.warning('Instance has no __next__ function in %s.', generator)
                else:
                    for typ in generator.py__iter__():
                        yield typ

    @property
    @underscore_memoization
    def name(self):
        return ContextName(self, self._class_context.name.string_name)

    def __repr__(self):
        return "<%s of %s(%s)>" % (self.__class__.__name__, self._class_context,
                                   self.var_args)


class LazyInstanceDict(object):
    def __init__(self, evaluator, instance, dct):
        self._evaluator = evaluator
        self._instance = instance
        self._dct = dct

    def __getitem__(self, name):
        return [get_instance_el(self._evaluator, self._instance, var, True)
                for var in self._dct[name]]

    def values(self):
        return [self[key] for key in self._dct]


class InstanceName(tree.Name):
    def __init__(self, origin_name, parent):
        super(InstanceName, self).__init__(origin_name.value,
                                           origin_name.start_pos)
        self._origin_name = origin_name
        self.parent = parent

    def is_definition(self):
        return self._origin_name.is_definition()


def get_instance_el(evaluator, instance, var, is_class_var=False):
    """
    Returns an InstanceElement if it makes sense, otherwise leaves the object
    untouched.

    Basically having an InstanceElement is context information. That is needed
    in quite a lot of cases, which includes Nodes like ``power``, that need to
    know where a self name comes from for example.
    """
    return var
    if isinstance(var, tree.Name):
        parent = get_instance_el(evaluator, instance, var.parent, is_class_var)
        return InstanceName(var, parent)
    elif var.type != 'funcdef' \
            and isinstance(var, (Instance, compiled.CompiledObject, tree.Leaf,
                           tree.Module, FunctionExecutionContext)):
        return var

    var = evaluator.wrap(var)
    return InstanceElement(evaluator, instance, var, is_class_var)


class InstanceElement(use_metaclass(CachedMetaClass, tree.Base)):
    """
    InstanceElement is a wrapper for any object, that is used as an instance
    variable (e.g. self.variable or class methods).
    """
    def __init__(self, evaluator, instance, var, is_class_var):
        self.evaluator = evaluator
        self.instance = instance
        self.var = var
        self.is_class_var = is_class_var

    @common.safe_property
    @memoize_default()
    def parent(self):
        par = self.var.parent
        if isinstance(par, ClassContext) and par == self.instance.base \
                or not isinstance(self.instance.base, (tree.Class, ClassContext)) \
                or isinstance(par, tree.Class) \
                and par == self.instance.base.base:
            par = self.instance
        else:
            par = get_instance_el(self.evaluator, self.instance, par,
                                  self.is_class_var)
        return par

    def get_parent_until(self, *args, **kwargs):
        return tree.BaseNode.get_parent_until(self, *args, **kwargs)

    def get_definition(self):
        return self.get_parent_until((tree.ExprStmt, tree.IsScope, tree.Import))

    def get_decorated_func(self):
        """ Needed because the InstanceElement should not be stripped """
        func = self.var.get_decorated_func()
        func = get_instance_el(self.evaluator, self.instance, func)
        return func

    def get_rhs(self):
        return get_instance_el(self.evaluator, self.instance,
                               self.var.get_rhs(), self.is_class_var)

    def is_definition(self):
        return self.var.is_definition()

    @property
    def children(self):
        # Copy and modify the array.
        return [get_instance_el(self.evaluator, self.instance, command, self.is_class_var)
                for command in self.var.children]

    @property
    @memoize_default()
    def name(self):
        return ContextName(self.var.name, self)

    def __iter__(self):
        for el in self.var.__iter__():
            yield get_instance_el(self.evaluator, self.instance, el,
                                  self.is_class_var)

    def __getitem__(self, index):
        return get_instance_el(self.evaluator, self.instance, self.var[index],
                               self.is_class_var)

    def isinstance(self, *cls):
        return isinstance(self.var, cls)

    def is_scope(self):
        """
        Since we inherit from Base, it would overwrite the action we want here.
        """
        return self.var.is_scope()

    def py__call__(self, params):
        if isinstance(self.var, compiled.CompiledObject):
            # This check is a bit strange, but CompiledObject itself is a bit
            # more complicated than we would it actually like to be.
            return self.var.py__call__(params)
        else:
            return FunctionContext.py__call__(self, params)

    def __repr__(self):
        return "<%s of %s>" % (self.__class__.__name__, self.var)


class Wrapper(tree.Base):
    def is_scope(self):
        return True

    def is_class(self):
        return False

    def py__bool__(self):
        """
        Since Wrapper is a super class for classes, functions and modules,
        the return value will always be true.
        """
        return True


class ClassContext(use_metaclass(CachedMetaClass, context.TreeContext, Wrapper)):
    """
    This class is not only important to extend `tree.Class`, it is also a
    important for descriptors (if the descriptor methods are evaluated or not).
    """
    def __init__(self, evaluator, classdef, parent_context):
        super(ClassContext, self).__init__(evaluator, parent_context=parent_context)
        self.classdef = classdef

    def get_node(self):
        return self.classdef

    @memoize_default(default=())
    def py__mro__(self):
        def add(cls):
            if cls not in mro:
                mro.append(cls)

        mro = [self]
        # TODO Do a proper mro resolution. Currently we are just listing
        # classes. However, it's a complicated algorithm.
        for lazy_cls in self.py__bases__():
            # TODO there's multiple different mro paths possible if this yields
            # multiple possibilities. Could be changed to be more correct.
            for cls in lazy_cls.infer():
                # TODO detect for TypeError: duplicate base class str,
                # e.g.  `class X(str, str): pass`
                try:
                    mro_method = cls.py__mro__
                except AttributeError:
                    # TODO add a TypeError like:
                    """
                    >>> class Y(lambda: test): pass
                    Traceback (most recent call last):
                      File "<stdin>", line 1, in <module>
                    TypeError: function() argument 1 must be code, not str
                    >>> class Y(1): pass
                    Traceback (most recent call last):
                      File "<stdin>", line 1, in <module>
                    TypeError: int() takes at most 2 arguments (3 given)
                    """
                    pass
                else:
                    add(cls)
                    for cls_new in mro_method():
                        add(cls_new)
        return tuple(mro)

    @memoize_default(default=())
    def py__bases__(self):
        arglist = self.classdef.get_super_arglist()
        if arglist:
            args = param.TreeArguments(self.evaluator, self, arglist)
            return [value for key, value in args.unpack() if key is None]
        else:
            return [context.LazyKnownContext(compiled.create(self.evaluator, object))]

    def py__call__(self, params):
        from jedi.evaluate.instance import TreeInstance
        return set([TreeInstance(self.evaluator, self.parent_context, self, params)])

    def py__class__(self):
        return compiled.create(self.evaluator, type)

    @property
    def params(self):
        try:
            return self.get_subscope_by_name('__init__').params
        except KeyError:
            return []  # object.__init__

    def names_dicts(self, search_global, is_instance=False):
        if search_global:
            yield self.names_dict
        else:
            for scope in self.py__mro__():
                if isinstance(scope, compiled.CompiledObject):
                    yield scope.names_dicts(False, is_instance)[0]
                else:
                    yield scope.names_dict

    def get_filters(self, search_global, until_position=None, origin_scope=None, is_instance=False):
        if search_global:
            yield ParserTreeFilter(self.evaluator, self, self.classdef, until_position, origin_scope=origin_scope)
        else:
            for scope in self.py__mro__():
                if isinstance(scope, compiled.CompiledObject):
                    for filter in scope.get_filters(is_instance=is_instance):
                        yield filter
                else:
                    yield ParserTreeFilter(self.evaluator, self, scope.classdef, origin_scope=origin_scope)

    def is_class(self):
        return True

    def get_subscope_by_name(self, name):
        raise DeprecationWarning
        for s in self.py__mro__():
            for sub in reversed(s.subscopes):
                if sub.name.value == name:
                    return sub
        raise KeyError("Couldn't find subscope.")

    def __repr__(self):
        return "<%s of %s>" % (self.__class__.__name__, self.classdef)

    @property
    def name(self):
        return ContextName(self, self.classdef.name)


class FunctionContext(use_metaclass(CachedMetaClass, context.TreeContext, Wrapper)):
    """
    Needed because of decorators. Decorators are evaluated here.
    """
    def __init__(self, evaluator, parent_context, funcdef):
        """ This should not be called directly """
        super(FunctionContext, self).__init__(evaluator, parent_context)
        self.base = self.base_func = self.funcdef = funcdef

    def get_node(self):
        return self.funcdef

    def names_dicts(self, search_global):
        if search_global:
            yield self.names_dict
        else:
            scope = self.py__class__()
            for names_dict in scope.names_dicts(False):
                yield names_dict

    def get_filters(self, search_global, until_position=None, origin_scope=None):
        if search_global:
            yield ParserTreeFilter(self.evaluator, self, self.base, until_position, origin_scope=origin_scope)
        else:
            scope = self.py__class__()
            for filter in scope.get_filters(search_global=False, origin_scope=origin_scope):
                yield filter

    def infer_function_execution(self, function_execution):
        """
        Created to be used by inheritance.
        """
        if self.base.is_generator():
            return set([iterable.Generator(self.evaluator, function_execution)])
        else:
            return function_execution.get_return_values()

    @Python3Method
    def py__call__(self, params):
        function_execution = FunctionExecutionContext(
            self.evaluator,
            self.parent_context,
            self.base,
            params
        )
        return self.infer_function_execution(function_execution)

    def py__class__(self):
        # This differentiation is only necessary for Python2. Python3 does not
        # use a different method class.
        if isinstance(self.base.get_parent_scope(), tree.Class):
            name = 'METHOD_CLASS'
        else:
            name = 'FUNCTION_CLASS'
        return compiled.get_special_object(self.evaluator, name)

    def __repr__(self):
        return "<%s of %s>" % (self.__class__.__name__, self.base_func)

    @property
    def name(self):
        return ContextName(self, self.funcdef.name)


class LambdaWrapper(FunctionContext):
    def get_decorated_func(self):
        return self


class FunctionExecutionContext(Executed):
    """
    This class is used to evaluate functions and their returns.

    This is the most complicated class, because it contains the logic to
    transfer parameters. It is even more complicated, because there may be
    multiple calls to functions and recursion has to be avoided. But this is
    responsibility of the decorators.
    """
    function_execution_filter = FunctionExecutionFilter

    def __init__(self, evaluator, parent_context, funcdef, var_args):
        super(FunctionExecutionContext, self).__init__(evaluator, parent_context, var_args)
        self.funcdef = funcdef
        if isinstance(funcdef, mixed.MixedObject):
            # The extra information in mixed is not needed anymore. We can just
            # unpack it and give it the tree object.
            funcdef = funcdef.definition

        # Just overwrite the old version. We don't need it anymore.
        #funcdef = helpers.deep_ast_copy(funcdef, new_elements=self._copy_dict)
        #for child in funcdef.children:
            #if child.type not in ('operator', 'keyword'):
                # Not all nodes are properly copied by deep_ast_copy.
                #child.parent = self
        #self.children = funcdef.children
        #self.names_dict = funcdef.names_dict
        #self._copied_funcdef = funcdef

    def get_node(self):
        return self.funcdef

    @memoize_default(default=set())
    @recursion.execution_recursion_decorator
    def get_return_values(self, check_yields=False):
        funcdef = self.funcdef
        if funcdef.type in ('lambdef', 'lambdef_nocond'):
            return self.evaluator.eval_element(self.children[-1])

        """
        if func.listeners:
            # Feed the listeners, with the params.
            for listener in func.listeners:
                listener.execute(self._get_params())
            # If we do have listeners, that means that there's not a regular
            # execution ongoing. In this case Jedi is interested in the
            # inserted params, not in the actual execution of the function.
            return set()
"""

        if check_yields:
            types = set()
            returns = funcdef.yields
        else:
            returns = funcdef.returns
            types = set(docstrings.find_return_types(self.evaluator, funcdef))
            types |= set(pep0484.find_return_types(self.evaluator, funcdef))

        for r in returns:
            check = flow_analysis.reachability_check(self, funcdef, r)
            if check is flow_analysis.UNREACHABLE:
                debug.dbg('Return unreachable: %s', r)
            else:
                if check_yields:
                    types |= set(self._eval_yield(r))
                else:
                    types |= self.eval_node(r.children[1])
            if check is flow_analysis.REACHABLE:
                debug.dbg('Return reachable: %s', r)
                break
        if check_yields:
            return context.get_merged_lazy_context(list(types))
        return types

    def _eval_yield(self, yield_expr):
        node = yield_expr.children[1]
        if node.type == 'yield_arg':
            # It must be a yield from.
            yield_from_types = self.eval_node(node)
            for lazy_context in iterable.py__iter__(self.evaluator, yield_from_types, node):
                yield lazy_context
        else:
            yield context.LazyTreeContext(self, node)

    @recursion.execution_recursion_decorator
    def get_yield_values(self):
        yields = self.funcdef.yields
        stopAt = tree.ForStmt, tree.WhileStmt, tree.IfStmt
        for_parents = [(x, x.get_parent_until((stopAt))) for x in yields]

        # Calculate if the yields are placed within the same for loop.
        yields_order = []
        last_for_stmt = None
        for yield_, for_stmt in for_parents:
            # For really simple for loops we can predict the order. Otherwise
            # we just ignore it.
            parent = for_stmt.parent
            if parent.type == 'suite':
                parent = parent.parent
            if for_stmt.type == 'for_stmt' and parent == self \
                    and for_stmt.defines_one_name():  # Simplicity for now.
                if for_stmt == last_for_stmt:
                    yields_order[-1][1].append(yield_)
                else:
                    yields_order.append((for_stmt, [yield_]))
            elif for_stmt == self:
                yields_order.append((None, [yield_]))
            else:
                yield self.get_return_values(check_yields=True)
                return
            last_for_stmt = for_stmt

        evaluator = self.evaluator
        for for_stmt, yields in yields_order:
            if for_stmt is None:
                # No for_stmt, just normal yields.
                for yield_ in yields:
                    for result in self._eval_yield(yield_):
                        yield result
            else:
                input_node = for_stmt.get_input_node()
                for_types = evaluator.eval_element(input_node)
                ordered = iterable.py__iter__(evaluator, for_types, input_node)
                for index_types in ordered:
                    dct = {str(for_stmt.children[1]): index_types}
                    evaluator.predefined_if_name_dict_dict[for_stmt] = dct
                    for yield_in_same_for_stmt in yields:
                        for result in self._eval_yield(yield_in_same_for_stmt):
                            yield result
                    del evaluator.predefined_if_name_dict_dict[for_stmt]

    def get_filters(self, search_global, until_position=None, origin_scope=None):
        yield self.function_execution_filter(self.evaluator, self, self.funcdef,
                                             until_position,
                                             origin_scope=origin_scope)

    @memoize_default(default=NO_DEFAULT)
    def get_params(self):
        return param.get_params(self.evaluator, self.parent_context, self.funcdef, self.var_args)

    def __repr__(self):
        return "<%s of %s>" % (self.__class__.__name__, self.funcdef)


class AnonymousFunctionExecution(FunctionExecutionContext):
    def __init__(self, evaluator, parent_context, funcdef):
        super(AnonymousFunctionExecution, self).__init__(
            evaluator, parent_context, funcdef, var_args=None)

    @memoize_default(default=NO_DEFAULT)
    def get_params(self):
        # We need to do a dynamic search here.
        return search_params(self.evaluator, self.parent_context, self.funcdef)


class ModuleAttributeName(AbstractNameDefinition):
    """
    For module attributes like __file__, __str__ and so on.
    """
    def __init__(self, parent_module, string_name):
        self.parent_context = parent_module
        self.string_name = string_name

    def infer(self):
        return compiled.create(self.parent_context.evaluator, str).execute(
            param.ValuesArguments([])
        )


class SubModuleName(AbstractNameDefinition):
    """
    """
    def __init__(self, parent_module, string_name):
        self.parent_context = parent_module
        self.string_name = string_name

    def infer(self):
        return imports.Importer(
            self.parent_context.evaluator,
            [self.string_name],
            self.parent_context,
            level=1
        ).follow()


class ModuleContext(use_metaclass(CachedMetaClass, context.TreeContext, Wrapper)):
    parent_context = None

    def __init__(self, evaluator, module_node):
        super(ModuleContext, self).__init__(evaluator, parent_context=None)
        self.module_node = module_node
        self.path = None

    def get_node(self):
        return self.module_node

    def names_dicts(self, search_global):
        yield self.base.names_dict
        yield self._module_attributes_dict()

        for star_module in self.star_imports():
            yield star_module.names_dict

        yield dict((str(n), [GlobalName(n)]) for n in self.base.global_names)
        yield self._sub_modules_dict()

    def get_filters(self, search_global, until_position=None, origin_scope=None):
        yield ParserTreeFilter(
            self.evaluator,
            self,
            self.module_node,
            until_position,
            origin_scope=origin_scope
        )
        yield GlobalNameFilter(self, self.module_node)
        yield DictFilter(self._sub_modules_dict())
        yield DictFilter(self._module_attributes_dict())
        for star_module in self.star_imports():
            yield next(star_module.get_filters(search_global))

    # I'm not sure if the star import cache is really that effective anymore
    # with all the other really fast import caches. Recheck. Also we would need
    # to push the star imports into Evaluator.modules, if we reenable this.
    #@cache_star_import
    @memoize_default([])
    def star_imports(self):
        modules = []
        for i in self.module_node.imports:
            if i.is_star_import():
                name = i.star_import_name()
                new = imports.ImportWrapper(self, name).follow()
                for module in new:
                    if isinstance(module, tree.Module):
                        modules += module.star_imports()
                modules += new
        return modules

    @memoize_default()
    def _module_attributes_dict(self):
        names = ['__file__', '__package__', '__doc__', '__name__']
        # All the additional module attributes are strings.
        return dict((n, ModuleAttributeName(self, n)) for n in names)

    @property
    @memoize_default()
    def name(self):
        return ContextName(self, self.module_node.name)

    def _get_init_directory(self):
        """
        :return: The path to the directory of a package. None in case it's not
                 a package.
        """
        for suffix, _, _ in imp.get_suffixes():
            ending = '__init__' + suffix
            py__file__ = self.py__file__()
            if py__file__ is not None and py__file__.endswith(ending):
                # Remove the ending, including the separator.
                return self.py__file__()[:-len(ending) - 1]
        return None

    def py__name__(self):
        for name, module in self.evaluator.modules.items():
            if module == self:
                return name

        return '__main__'

    def py__file__(self):
        """
        In contrast to Python's __file__ can be None.
        """
        if self.module_node.path is None:
            return None

        return os.path.abspath(self.module_node.path)

    def py__package__(self):
        if self._get_init_directory() is None:
            return re.sub(r'\.?[^\.]+$', '', self.py__name__())
        else:
            return self.py__name__()

    def _py__path__(self):
        search_path = self.evaluator.sys_path
        init_path = self.py__file__()
        if os.path.basename(init_path) == '__init__.py':
            with open(init_path, 'rb') as f:
                content = common.source_to_unicode(f.read())
                # these are strings that need to be used for namespace packages,
                # the first one is ``pkgutil``, the second ``pkg_resources``.
                options = ('declare_namespace(__name__)', 'extend_path(__path__')
                if options[0] in content or options[1] in content:
                    # It is a namespace, now try to find the rest of the
                    # modules on sys_path or whatever the search_path is.
                    paths = set()
                    for s in search_path:
                        other = os.path.join(s, unicode(self.name))
                        if os.path.isdir(other):
                            paths.add(other)
                    return list(paths)
        # Default to this.
        return [self._get_init_directory()]

    @property
    def py__path__(self):
        """
        Not seen here, since it's a property. The callback actually uses a
        variable, so use it like::

            foo.py__path__(sys_path)

        In case of a package, this returns Python's __path__ attribute, which
        is a list of paths (strings).
        Raises an AttributeError if the module is not a package.
        """
        path = self._get_init_directory()

        if path is None:
            raise AttributeError('Only packages have __path__ attributes.')
        else:
            return self._py__path__

    @memoize_default()
    def _sub_modules_dict(self):
        """
        Lists modules in the directory of this module (if this module is a
        package).
        """
        path = self.module_node.path
        names = {}
        if path is not None and path.endswith(os.path.sep + '__init__.py'):
            mods = pkgutil.iter_modules([os.path.dirname(path)])
            for module_loader, name, is_pkg in mods:
                # It's obviously a relative import to the current module.
                names[name] = SubModuleName(self, name)

        # TODO add something like this in the future, its cleaner than the
        #   import hacks.
        # ``os.path`` is a hardcoded exception, because it's a
        # ``sys.modules`` modification.
        # if str(self.name) == 'os':
        #     names.append(Name('path', parent_context=self))

        return names

    def py__class__(self):
        return compiled.get_special_object(self.evaluator, 'MODULE_CLASS')

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self.module_node)
