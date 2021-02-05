import logging
from dataclasses import dataclass
from typing import List, Dict, Hashable, Optional, Tuple, Generator
from functools import reduce

from dask.core import get_deps, toposort

from metagraph.core.plugin import ConcreteAlgorithm, Compiler, CompileError
from metagraph.core.dask.placeholder import DelayedAlgo


@dataclass
class DaskSubgraph:
    """A subgraph of a larger Dask task graph.

    Currently subgraph must have 0 or more inputs and only 1 output
    """

    tasks: dict
    input_keys: List[Hashable]
    output_key: Hashable


def extract_compilable_subgraphs(
    dsk: Dict, compiler: str, include_singletons=True
) -> List[DaskSubgraph]:
    """Find compilable subgraphs in this Dask task graph.

    Currently only works with one compiler at a time, and only will return
    linear chains of compilable tasks.  If include_singletons is True,
    returned chains may be of length 1.  If False, the chain length must be >1.
    """

    if include_singletons:
        chain_threshold = 1
    else:
        chain_threshold = 2
    dependencies, dependents = get_deps(dsk)

    compilable_keys, non_compilable_keys = _get_compilable_dask_keys(dsk, compiler)

    if len(compilable_keys) == 0:
        return []

    subgraphs = []
    ordered_keys = toposort(compilable_keys, dependencies=dependencies)
    current_chain = [ordered_keys[0]]

    def _note_subgraph(chain):
        output_key = chain[-1]
        chain = set(chain)
        inputs = reduce(
            set.union, (dependencies[chain_key] - chain for chain_key in chain)
        )
        tasks = {chain_key: dsk[chain_key] for chain_key in chain}
        subgraphs.append(
            DaskSubgraph(tasks=tasks, input_keys=list(inputs), output_key=output_key)
        )

    for key, next_key in zip(ordered_keys[:-1], ordered_keys[1:]):
        key_dependents = dependents[key]
        next_key_dependencies = dependencies[next_key]

        if (
            len(key_dependents) == 1
            and len(next_key_dependencies) == 1
            and key in next_key_dependencies
            and next_key in key_dependents
        ):
            current_chain.append(next_key)
        elif len(current_chain) >= chain_threshold:
            _note_subgraph(current_chain)
            current_chain = [next_key]
        else:
            current_chain = [next_key]
        key = next_key

    if len(current_chain) >= chain_threshold:
        _note_subgraph(current_chain)

    return subgraphs


def _get_compilable_dask_keys(dsk: Dict, compiler: str) -> Tuple[set, set]:

    compilable_keys = set()
    for key in dsk.keys():
        task_callable = dsk[key][0]
        if isinstance(task_callable, DelayedAlgo):
            if task_callable.algo._compiler == compiler:
                compilable_keys.add(key)

    non_compilable_keys = set(dsk.keys()) - compilable_keys

    return compilable_keys, non_compilable_keys


def compile_subgraphs(dsk, keys, compiler: Compiler):
    """Return a modified dask graph with compilable subgraphs fused together."""

    subgraphs = extract_compilable_subgraphs(dsk, compiler=compiler.name)
    if len(subgraphs) == 0:
        return dsk  # no change, nothing to compile

    # make a new graph we can mutate
    new_dsk = dsk.copy()
    for subgraph in subgraphs:
        try:
            fused_func = compiler.compile_subgraph(
                subgraph.tasks, subgraph.input_keys, subgraph.output_key
            )

            # remove keys for existing tasks in subgraph, including the output task
            for key in subgraph.tasks:
                del new_dsk[key]

            # put the fused task in with the output task's old key
            new_dsk[subgraph.output_key] = (fused_func, *subgraph.input_keys)
        except CompileError as e:
            logging.debug(
                "Unable to compile subgraph with output key: %s",
                subgraph.output_key,
                exc_info=e,
            )
            # continue with graph unchanged to next subgraph

    return new_dsk


def optimize(dsk, keys, *, compiler: Optional[Compiler] = None, **kwargs):
    """Top level optimizer function for Metagraph DAGs"""
    # FUTURE: swap nodes in graph with compilable implementations if they exist?

    if compiler is not None:
        optimized_dsk = compile_subgraphs(dsk, keys, compiler=compiler)

    return optimized_dsk
