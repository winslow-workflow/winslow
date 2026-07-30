class FilterQuery:
    def __init__(self, root):
        self.root = root

    def apply(self, tasks):
        return [t for t in tasks if self.root.evaluate(t)]

    def explain(self):
        return self.root.explain()
