class FilterQuery:
    def __init__(self, root):
        self.root = root

    def apply(self, tasks):
        return [t for t in tasks if self.root.evaluate(t)]

    def filters(self):
        """The leaf filter instances, so the history search can refuse a
        query with a filter outside BUILTIN_FILTERS."""
        return self.root.leaves()

    def explain(self):
        return self.root.explain()
