from winslow.filter import TaskFilter


class DependsOnFilter(TaskFilter):
    short_command = "n"
    long_command = "needs"

    def matches(self, task):
        return any(self.value.lower() in str(dep) for dep in task.dependent_tasks)

    def explain(self):
        return f"depends on {self.value}"
