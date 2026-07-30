from winslow.registry import Registry
from .workflow import Workflow


class WorkflowRegistry(Registry):
    item_class = Workflow
    file_filter = {"workflow.py"}
