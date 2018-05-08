# This is an adaptation by Taddeus Kroes of
# https://github.com/nicolashainaux/sphinx-autodoc-annotation, which was
# originally developed by Virgil Dupras and Nicolas Hainaux.

import inspect
from typing import Union
from sphinx.ext.autodoc import FunctionDocumenter, MethodDocumenter


def typestr(obj):
    if obj is None or obj == inspect.Signature.empty:
        return

    if obj is type(None):
        return 'None'

    if isinstance(obj, str):
        return obj

    assert hasattr(obj, '__module__')

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
                a, b = obj.__union_params__
            else:
                a, b = obj.__args__  # Python 3.6
            return typestr(a) + ' or ' + typestr(b)

        if classname in ('List', 'Dict', 'Iterator', 'Iterable'):
            args = ', '.join(typestr(t) for t in obj.__args__)
            return '%s[%s]' % (classname, args)

        return str(obj)

    mod = obj.__module__

    # Strip nested modules if the class is exported at the toplevel
    while '.' in mod:
        basemod, nestedmod = mod.rsplit('.', 1)
        try:
            imported_class = getattr(__import__(basemod), classname)
            assert imported_class is obj
            print('{mod}.{classname} -> {basemod}.{classname}'.format(**locals()))
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


def add_annotation_content(obj, result):
    try:
        sig = inspect.signature(obj)
    except ValueError:
        # Can't extract signature, do nothing
        return

    existing_contents = '\n'.join(result)
    toadd = []

    for param in sig.parameters.values():
        type_directive = ':type %s:' % param.name

        if type_directive in existing_contents:
            # We already specidy the type of that argument in the docstring,
            # don't specify it again.
            continue

        arg_link = typestr(get_param_type(param))
        if arg_link:
            toadd.append('%s %s' % (type_directive, arg_link))

    if sig.return_annotation != inspect.Signature.empty:
        if ':rtype:' not in existing_contents:
            toadd.append(':rtype: %s' % typestr(sig.return_annotation))

    if toadd:
        # Let's see where we're going to insert our directives. We can't append
        # it at the end of the docstring because there might be a section
        # breaker between our params and the end of the list that will also
        # break our :type: stuff. We have to try to keep them grouped.
        for i, s in enumerate(result):
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
            insert_index = len(result)

        result[insert_index:insert_index] = toadd


def process_docstring(app, what, name, obj, options, lines):
    if what in ('function', 'method', 'class'):
        add_annotation_content(obj, lines)


def process_signature(app, what, name, obj, options, signature, return_annotation):
    if what in ('function', 'method', 'class'):
        # Fix for concatenated class/__init__ docstrings where the parameter
        # type is added to the class docstring but the signature information
        # has to come from __init__
        if what == 'class':
            params = list(inspect.signature(obj.__init__).parameters.values())[1:]
        else:
            params = inspect.signature(obj).parameters.values()

        stripped_params = []
        for p in params:
            stripped_params.append(inspect.Parameter(p.name, p.kind, default=p.default))

        newsig = inspect.Signature(stripped_params)

        return str(newsig), None


def setup(app):
    app.connect('autodoc-process-docstring', process_docstring)
    app.connect('autodoc-process-signature', process_signature)
