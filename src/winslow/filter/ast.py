class FilterNode:
    def evaluate(self, task) -> bool:
        raise NotImplementedError

    def explain(self) -> str:
        raise NotImplementedError

    def flatten(self):
        return self

    def leaves(self):
        """Every filter instance under this node, in syntax order."""
        raise NotImplementedError


class LeafNode(FilterNode):
    def __init__(self, filter_instance):
        self.filter = filter_instance

    def evaluate(self, task):
        return self.filter.matches(task)

    def explain(self):
        return self.filter.explain()

    def leaves(self):
        return (self.filter,)


class NotNode(FilterNode):
    def __init__(self, child):
        self.child = child

    def evaluate(self, task):
        return not self.child.evaluate(task)

    def explain(self):
        return f"not ({self.child.explain()})"

    def flatten(self):
        return NotNode(self.child.flatten())

    def leaves(self):
        return self.child.leaves()


class AndNode(FilterNode):
    def __init__(self, children):
        self.children = children

    def evaluate(self, task):
        return all(child.evaluate(task) for child in self.children)

    def explain(self):
        parts = []
        for child in self.children:
            # OR binds less tightly than AND. Put it in parentheses to keep the
            # English clear.
            explained = (
                f"({child.explain()})" if isinstance(child, OrNode) else child.explain()
            )
            parts.append(explained)
        return " and ".join(parts)

    def flatten(self):
        children = []
        for child in self.children:
            flat = child.flatten()
            if isinstance(flat, AndNode):
                children.extend(flat.children)
            else:
                children.append(flat)
        return AndNode(children)

    def leaves(self):
        return tuple(f for child in self.children for f in child.leaves())


class OrNode(FilterNode):
    def __init__(self, children):
        self.children = children

    def evaluate(self, task):
        return any(child.evaluate(task) for child in self.children)

    def explain(self):
        return " or ".join(child.explain() for child in self.children)

    def flatten(self):
        children = []
        for child in self.children:
            flat = child.flatten()
            if isinstance(flat, OrNode):
                children.extend(flat.children)
            else:
                children.append(flat)
        return OrNode(children)

    def leaves(self):
        return tuple(f for child in self.children for f in child.leaves())
