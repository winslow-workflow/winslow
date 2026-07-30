import re
from enum import Enum

from .ast import AndNode, OrNode, NotNode, LeafNode
from .query import FilterQuery


class Token(Enum):
    AND = "AND"
    OR = "OR"
    NOT = "NOT"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    COMMA = "COMMA"
    CMD = "CMD"
    WORD = "WORD"
    EOF = "EOF"


_TOKEN_RE = re.compile(
    r"(?P<AND>&)"
    r"|(?P<OR>\|)"
    r"|(?P<NOT>~)"
    r"|(?P<LPAREN>\()"
    r"|(?P<RPAREN>\))"
    r"|(?P<COMMA>,)"
    r"|!(?P<CMD>[\w-]+)"
    r"|(?P<WORD>[\w-]+)"
    r"|\s+"
)


def _tokenize(query):
    def to_token(m):
        match m.lastgroup:
            case "CMD":
                return (Token.CMD, m.group("CMD"))
            case "WORD":
                word = m.group()
                match word.lower():
                    case "and":
                        return (Token.AND, None)
                    case "or":
                        return (Token.OR, None)
                    case "not":
                        return (Token.NOT, None)
                    case _:
                        return (Token.WORD, word)
            case "AND":
                return (Token.AND, None)
            case "OR":
                return (Token.OR, None)
            case "NOT":
                return (Token.NOT, None)
            case "LPAREN":
                return (Token.LPAREN, None)
            case "RPAREN":
                return (Token.RPAREN, None)
            case "COMMA":
                return (Token.COMMA, None)
            case _:
                return None  # whitespace

    return [t for m in _TOKEN_RE.finditer(query) if (t := to_token(m))] + [
        (Token.EOF, None)
    ]


class TokenStream:
    def __init__(self, tokens):
        self._tokens = tokens
        self._pos = 0

    def peek(self):
        return self._tokens[self._pos]

    def advance(self):
        token = self._tokens[self._pos]
        self._pos += 1
        return token

    def expect(self, type_):
        token = self.advance()
        if token[0] != type_:
            raise ValueError(
                f"Expected {type_.value} but got {token[0].value} ('{token[1]}')"
            )
        return token


# This limits the recursion. Deep nesting, such as '((((' or '~~~~', thus raises
# the ValueError of the parser. A RecursionError would escape the `except
# ValueError` of each caller. The limit is much deeper than a real filter.
MAX_FILTER_DEPTH = 100


class FilterParser:
    def __init__(self, registry):
        self.registry = registry

    def parse(self, query_string):
        stream = TokenStream(_tokenize(query_string))
        root = self._expr(stream, 0)
        if stream.peek()[0] != Token.EOF:
            raise ValueError(
                f"Unexpected token '{stream.peek()[1]}' in: {query_string!r}"
            )
        return FilterQuery(root.flatten())

    def _expr(self, stream, depth):
        # expr := term ('|' term)*
        children = [self._term(stream, depth)]
        while stream.peek()[0] == Token.OR:
            stream.advance()
            children.append(self._term(stream, depth))
        return children[0] if len(children) == 1 else OrNode(children)

    def _term(self, stream, depth):
        # term := atom ('&' atom)*
        children = [self._atom(stream, depth)]
        while stream.peek()[0] == Token.AND:
            stream.advance()
            children.append(self._atom(stream, depth))
        return children[0] if len(children) == 1 else AndNode(children)

    def _atom(self, stream, depth):
        # atom := '~' atom | '(' expr ')' | filter
        # '~' negates the name or the group after it. It must not come directly
        # before a command, because a command annotates a type. Negate the value
        # of the command instead: use `!g ~foo` and not `~!g foo`.
        if depth > MAX_FILTER_DEPTH:
            raise ValueError(
                f"filter expression nested too deeply (over {MAX_FILTER_DEPTH} levels)"
            )
        match stream.peek():
            case (Token.NOT, _):
                stream.advance()
                if (nxt := stream.peek())[0] == Token.CMD:
                    raise ValueError(
                        f"Negation must follow the filter command, not precede it - "
                        f"write '!{nxt[1]} ~value' instead of '~!{nxt[1]} value'"
                    )
                return NotNode(self._atom(stream, depth + 1))
            case (Token.LPAREN, _):
                stream.advance()
                node = self._expr(stream, depth + 1)
                stream.expect(Token.RPAREN)
                return node
            case _:
                return self._filter(stream)

    def _filter(self, stream):
        # filter := CMD '~'? values | values    (values := WORD (',' WORD)*)
        match stream.peek():
            case (Token.CMD, cmd):
                stream.advance()
                filter_cls = self.registry.resolve(cmd)
            case (Token.WORD, _):
                filter_cls = self.registry.default
            case (tok, _):
                raise ValueError(f"Expected a filter expression but got {tok.value!r}")

        # The negation comes after the command annotator, which is optional:
        # !g ~foo.
        negate = stream.peek()[0] == Token.NOT
        if negate:
            stream.advance()

        node = self._comma_or(filter_cls, stream)
        return NotNode(node) if negate else node

    def _comma_or(self, filter_cls, stream):
        # foo,bar,baz expands into OrNode([foo, bar, baz]).
        _, first = stream.expect(Token.WORD)
        values = [first]
        while stream.peek()[0] == Token.COMMA:
            stream.advance()
            _, value = stream.expect(Token.WORD)
            values.append(value)
        nodes = [LeafNode(filter_cls(v)) for v in values]
        return nodes[0] if len(nodes) == 1 else OrNode(nodes)
