import dask
from dask import is_dask_collection
from dask.base import DaskMethodsMixin, tokenize
from dask.core import quote
from dask.highlevelgraph import HighLevelGraph


def single_key(seq):
    return seq[0]


def rebuild(dsk, cls, key):
    return cls(key, dsk)


def ph_apply(func, args, kwargs):
    return func(*args, **kwargs)


def finalize(collection):
    assert is_dask_collection(collection)

    if isinstance(collection, Placeholder):
        return collection._key, collection._dsk

    name = "finalize-" + tokenize(collection)
    keys = collection.__dask_keys__()
    finalize, args = collection.__dask_postcompute__()
    layer = {name: (finalize, keys) + args}
    graph = HighLevelGraph.from_collections(name, layer, dependencies=[collection])
    return name, graph


class Placeholder(DaskMethodsMixin):
    concrete_type = None  # subclasses should override this
    __dask_scheduler__ = staticmethod(dask.threaded.get)

    def __init__(self, key, dsk=None):
        self._key = key
        if dsk is None:
            dsk = {}
        self._dsk = dsk

    def __dask_graph__(self):
        return self._dsk

    def __dask_keys__(self):
        return [self._key]

    def __dask_tokenize__(self):
        return self._key

    def __dask_postcompute__(self):
        return single_key, ()

    def __dask_postpersist__(self):
        return rebuild, (self.__class__, self._key)

    @classmethod
    def build(cls, key, func, args, kwargs=None):
        dsk = {}
        new_args = []
        for arg in args:
            if is_dask_collection(arg):
                arg, graph = finalize(arg)
                dsk.update(graph)
            else:
                arg = quote(arg)
            new_args.append(arg)
        if kwargs:
            new_kwargs_flat = []
            for kw, val in kwargs.items():
                if is_dask_collection(val):
                    val, graph = finalize(val)
                    dsk.update(graph)
                else:
                    val = quote(val)
                new_kwargs_flat.append([kw, val])
            # Add this func to the task graph
            dsk[key] = (ph_apply, func, new_args, (dict, new_kwargs_flat))
        else:
            # Add this func to the task graph (no need for apply)
            dsk[key] = (func,) + tuple(new_args)
        return cls(key, dsk)
