# This is an adaptation by Taddeus Kroes of
# https://github.com/nicolashainaux/sphinx-autodoc-annotation, which was
# originally developed by Virgil Dupras and maintained by Nicolas Hainaux.
# It has been tested pn Python 3.5 and 3.6.

import inspect
from importlib import import_module
from typing import Union, _ForwardRef, Any, NewType, get_type_hints
from sphinx.ext.autodoc import FunctionDocumenter, MethodDocumenter


def typestr(obj):
    if obj is None or obj == inspect.Signature.empty:
        return

    if obj is type(None):
        return 'None'

    if isinstance(obj, str):
        return obj

    assert hasattr(obj, '__module__')

    if isinstance(obj, _ForwardRef):
        # Cannot evaluate in current namespace, just return the string literal
        # instead
        #obj = obj._eval_type(globals(), locals())
        return str(obj)[len("_ForwardRef('"):-len("')")]

    if obj is Any:
        return 'Any'

    # Don't show NewType typenames, they should only be used for type checking,
    # just link to the referenced supertype here
    if obj.__module__ == 'typing' and hasattr(obj, '__qualname__') and \
            obj.__qualname__.startswith('NewType.'):
        return typestr(obj.__supertype__)

    if inspect.isclass(obj):
        classname = obj.__qualname__
    else:
        # Fix for python 3.6 where typing types are not classes
        assert obj.__module__ == 'typing'
        assert hasattr(obj, '__origin__')
        classname = str(obj.__origin__).replace('typing.', '')

    if obj.__module__ == 'builtins':
        return classname

    if obj.__module__ == 'typing':
        if classname in ('Union', 'Optional'):
            if hasattr(obj, '__union_params__'):
                a, b = obj.__union_params__  # Python 3.5
            else:
                a, b = obj.__args__  # Python 3.6
            return typestr(a) + ' or ' + typestr(b)

        if classname == 'Callable':
            if hasattr(obj, '__result__'):
                # Python 3.5
                params = obj.__args__
                ret = obj.__result__
            else:
                # Python 3.6
                params = obj.__args__[:-1]
                ret = obj.__args__[-1]
            params = ', '.join(typestr(t) for t in params)
            return 'callable[(%s) -> %s]' % (params, typestr(ret))

        if classname in ('List', 'Tuple', 'Dict', 'Iterator', 'Iterable'):
            if hasattr(obj, '__tuple_params__'):
                args = obj.__tuple_params__  # Python 3.5
            else:
                args = obj.__args__  # Python 3.6

            args = ', '.join(typestr(t) for t in args)
            return '%s[%s]' % (classname.lower(), args)

        return str(obj)

    mod = obj.__module__

    # Strip nested module names if the class is exported in __init__.py
    while '.' in mod:
        basemod, nestedmod = mod.rsplit('.', 1)
        try:
            imported_class = getattr(import_module(basemod), classname)
            assert imported_class is obj
            mod = basemod
        except AttributeError:
            break

    fullname = '%s.%s' % (mod, classname)

    # Strip the name of the package being documented (or it will be in every
    # link)
    fullname = fullname.replace('infra.', '')

    return fullname


def get_param_type(param):
    if param.annotation != inspect.Signature.empty:
        return param.annotation
    else:
        # We don't want to overreach ourselves. Too many possibilities of
        # messing up. So, we only support basic types here.
        if isinstance(param.default, (bool, int, float, str)):
            return type(param.default)


def get_classvar_annotation(fullname, existing_contents):
    modname, classname, attrname = fullname.rsplit('.', 2)
    mod = import_module(modname)
    cls = getattr(mod, classname)
    if hasattr(cls, '__annotations__') and attrname in cls.__annotations__:
        ty = cls.__annotations__[attrname]
        # FIXME: no nice annotation format for this?
        return ['**Type**: :any:`%s`' % typestr(ty)]


def get_callable_annotation(obj, existing_contents):
    try:
        sig = inspect.signature(obj)
    except TypeError:
        print('unsupported type:', obj)
        return
    except ValueError:
        # Object does not have a signature
        return

    toadd = []

    for param in sig.parameters.values():
        type_directive = ':type %s:' % param.name

        if type_directive in existing_contents:
            # We already specify the type of that argument in the docstring,
            # don't specify it again.
            continue

        arg_link = typestr(get_param_type(param))
        if arg_link:
            toadd.append('%s %s' % (type_directive, arg_link))

    if sig.return_annotation != inspect.Signature.empty:
        if ':rtype:' not in existing_contents:
            toadd.append(':rtype: %s' % typestr(sig.return_annotation))

    return toadd


def add_lines(toadd, lines, existing_contents):
    # Let's see where we're going to insert our directives. We can't append
    # it at the end of the docstring because there might be a section
    # breaker between our params and the end of the list that will also
    # break our :type: stuff. We have to try to keep them grouped.
    for i, s in enumerate(lines):
        # TODO: do somewhting nicer with sorting, params before :returns:,
        # :rtype: directly after
        if s.startswith(':raises '):
            insert_index = i
            break

        if s.startswith(':'):
            insert_index = i + 1
    else:
        # We don't have a metadata directive, just insert at the end and
        # hope for the best
        # FIXME: wtf is a section breaker?
        insert_index = len(lines)

    lines[insert_index:insert_index] = toadd


def process_docstring(app, what, name, obj, options, lines):
    if what in ('attribute', 'function', 'method', 'class'):
        existing_contents = '\n'.join(lines)

        if what == 'attribute':
            toadd = get_classvar_annotation(name, existing_contents)
        else:
            toadd = get_callable_annotation(obj, existing_contents)

        if toadd:
            add_lines(toadd, lines, existing_contents)



def process_signature(app, what, name, obj, options, signature, return_annotation):
    if what in ('function', 'method', 'class'):
        # Fix for concatenated class/__init__ docstrings where the parameter
        # type is added to the class docstring but the signature information
        # has to come from __init__
        if what == 'class':
            params = list(inspect.signature(obj.__init__).parameters.values())[1:]
        else:
            params = inspect.signature(obj).parameters.values()

        stripped_params = [p.replace(annotation=inspect.Parameter.empty) for p in params]
        newsig = inspect.Signature(stripped_params)

        return str(newsig), None


def setup(app):
    app.connect('autodoc-process-docstring', process_docstring)
    app.connect('autodoc-process-signature', process_signature)
