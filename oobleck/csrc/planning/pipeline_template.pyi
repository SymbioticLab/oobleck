from typing import Dict, List, Tuple

class LayerExecutionResult:
    def __init__(
        self,
        layer_index: int,
        forward: float,
        backward: float,
        allreduce_in_node: Dict[int, float],
        allreduce_across_nodes: Dict[int, float],
        mem_required: Tuple[int, int],
    ): ...
    _index: int
    _forward: float
    _backward: float
    _allreduce_in_node: Dict[int, float]
    _allreduce_across_nodes: Dict[int, float]
    _mem_required: Tuple[int, int]

class LayerExecutionResults:
    def get(self) -> List[LayerExecutionResult]: ...
    def at(self, index: int) -> LayerExecutionResult: ...
    def size(self) -> int: ...

class StageExecutionResult:
    def __init__(
        self,
        LayerExecutionResults,
        layer_indices: Tuple[int, int],
        num_gpus: int,
    ): ...
    _num_gpus: int
    _layer_indices: List[int]
    _size: int
    _mem_required: int

def get_profile_results(
    model_name: str, model_tag: str, microbatch_size: int
) -> LayerExecutionResults: ...

class PipelineTemplate:
    def __init__(
        self,
        stages: List[StageExecutionResult],
        iteration_time: float,
        num_layers: int,
        num_nodes: int,
        num_gpus_per_node: int,
    ): ...
    _num_nodes: int
    _num_gpus_per_node: int
    _stages: List[StageExecutionResult]
    _iteration_time: float
    def get_pipeline_ranks(self, start_rank: int, fsdp_index: int):
        list[int]: ...
    def get_layer_ranks(self, start_rank: int, layer_index: int):
        list[int]: ...

class PipelineTemplateGenerator:
    def __init__(self): ...
    def create_pipeline_templates(
        self,
        layer_execution_results: LayerExecutionResults,
        num_nodes: Tuple[int, int],
        num_gpus_per_node: int,
    ) -> List[PipelineTemplate]: ...
