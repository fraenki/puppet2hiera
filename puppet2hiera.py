#!/usr/bin/env python3
"""puppet2hiera.py — Puppet Manifest → Hiera 5 YAML Converter.

Converts Puppet resource declarations and class declarations into
Hiera 5 compatible YAML format, preserving quoting style from the
original Puppet code.

Usage:
    python3 puppet2hiera.py <input_file> [output_file]
    python3 puppet2hiera.py -                    # read from stdin
    cat manifest.pp | python3 puppet2hiera.py -  # pipe
"""

import re
import sys
from collections import OrderedDict


# =============================================================================
# Tokenizer (Lexer)
# =============================================================================

# Token types
TOKEN_SINGLE_STRING = 'SINGLE_STRING'
TOKEN_DOUBLE_STRING = 'DOUBLE_STRING'
TOKEN_HASHROCKET = 'HASHROCKET'
TOKEN_LBRACE = 'LBRACE'
TOKEN_RBRACE = 'RBRACE'
TOKEN_LBRACKET = 'LBRACKET'
TOKEN_RBRACKET = 'RBRACKET'
TOKEN_COMMA = 'COMMA'
TOKEN_COLON = 'COLON'
TOKEN_IDENT = 'IDENT'
TOKEN_NUMBER = 'NUMBER'

# Regex patterns for tokenization — order matters
TOKEN_PATTERNS = [
    # Skip comments (# to end of line)
    (None, r'#[^\n]*'),
    # Skip whitespace
    (None, r'\s+'),
    # Single-quoted strings (no escape sequences except \\ and \')
    (TOKEN_SINGLE_STRING, r"'(?:[^'\\]|\\.)*'"),
    # Double-quoted strings (supports escape sequences)
    (TOKEN_DOUBLE_STRING, r'"(?:[^"\\]|\\.)*"'),
    # Hashrocket =>
    (TOKEN_HASHROCKET, r'=>'),
    # Braces and brackets
    (TOKEN_LBRACE, r'\{'),
    (TOKEN_RBRACE, r'\}'),
    (TOKEN_LBRACKET, r'\['),
    (TOKEN_RBRACKET, r'\]'),
    # Comma
    (TOKEN_COMMA, r','),
    # Colon
    (TOKEN_COLON, r':'),
    # Numbers (integers and floats, including negative)
    (TOKEN_NUMBER, r'-?\d+\.\d+|-?\d+'),
    # Identifiers (bare words: type names, param names, true/false/undef)
    # Includes :: for qualified Puppet class names
    (TOKEN_IDENT, r'[a-zA-Z_][a-zA-Z0-9_]*(?:::[a-zA-Z_][a-zA-Z0-9_]*)*'),
]

# Compile all patterns into one master regex
_MASTER_PATTERN = '|'.join(
    '(?P<{}>{})'.format(
        tok_type or '_SKIP_{}'.format(i),
        pattern,
    )
    for i, (tok_type, pattern) in enumerate(TOKEN_PATTERNS)
)
_MASTER_RE = re.compile(_MASTER_PATTERN)


def tokenize(source):
    """Tokenize Puppet source code into a list of (type, value) tuples.

    Skips whitespace and comments. Raises ValueError on unexpected characters.
    """
    tokens = []
    pos = 0
    for match in _MASTER_RE.finditer(source):
        # Check for gaps (unexpected characters between matches)
        if match.start() != pos:
            unexpected = source[pos:match.start()].strip()
            if unexpected:
                raise ValueError(
                    'Unexpected character(s) at position {}: {!r}'.format(
                        pos, unexpected,
                    )
                )
        pos = match.end()

        # Determine which group matched
        for tok_type, _ in TOKEN_PATTERNS:
            if tok_type is None:
                continue
            value = match.group(tok_type)
            if value is not None:
                tokens.append((tok_type, value))
                break

    # Check for trailing unexpected characters
    remaining = source[pos:].strip()
    if remaining:
        raise ValueError(
            'Unexpected character(s) at position {}: {!r}'.format(
                pos, remaining,
            )
        )

    return tokens


# =============================================================================
# Parser (Recursive Descent)
# =============================================================================

class Parser:
    """Recursive descent parser for Puppet resource/class declarations.

    Produces a list of declarations, each as:
        (type_name, title, OrderedDict_of_params)

    Values are stored as typed tuples to preserve quoting information:
        ('single', 'text')    — single-quoted string
        ('double', 'text')    — double-quoted string
        ('bare', 'true')      — bare boolean/identifier
        ('number', '42')      — numeric literal
        ('hash', OrderedDict)  — hash
        ('array', list)        — array
        ('undef', None)        — undef
    """

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        """Return current token without consuming it, or None at end."""
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def consume(self, expected_type=None):
        """Consume and return current token. Optionally assert its type."""
        token = self.peek()
        if token is None:
            raise ValueError(
                'Unexpected end of input, expected {}'.format(expected_type)
            )
        if expected_type and token[0] != expected_type:
            raise ValueError(
                'Expected {}, got {} ({!r}) at token {}'.format(
                    expected_type, token[0], token[1], self.pos,
                )
            )
        self.pos += 1
        return token

    def at_end(self):
        """Return True if all tokens have been consumed."""
        return self.pos >= len(self.tokens)

    # --- Grammar rules ---

    def parse(self):
        """Parse the entire file: zero or more declarations."""
        declarations = []
        while not self.at_end():
            declarations.append(self.parse_declaration())
        return declarations

    def parse_declaration(self):
        """Parse one resource or class declaration.

        resource_decl : IDENT '{' string ':' param_list '}'
        class_decl    : 'class' '{' string ':' param_list '}'
        """
        tok = self.consume(TOKEN_IDENT)
        type_name = tok[1]

        self.consume(TOKEN_LBRACE)
        title_tok = self.parse_string_token()
        # Strip surrounding quotes to get the inner text
        title = title_tok[1][1:-1]
        self.consume(TOKEN_COLON)
        params = self.parse_param_list()
        self.consume(TOKEN_RBRACE)

        return (type_name, title, params)

    def parse_string_token(self):
        """Parse a single-quoted or double-quoted string, return raw token."""
        tok = self.peek()
        if tok and tok[0] in (TOKEN_SINGLE_STRING, TOKEN_DOUBLE_STRING):
            return self.consume()
        raise ValueError(
            'Expected string, got {} ({!r}) at token {}'.format(
                tok[0] if tok else 'EOF',
                tok[1] if tok else '',
                self.pos,
            )
        )

    def parse_param_list(self):
        """Parse parameter list: (param (',' param)* ','?)?

        Returns an OrderedDict of param_name -> typed_value.
        """
        params = OrderedDict()
        # Check if we're at the closing brace (empty param list)
        while True:
            tok = self.peek()
            if tok is None or tok[0] == TOKEN_RBRACE:
                break
            name_tok = self.consume(TOKEN_IDENT)
            self.consume(TOKEN_HASHROCKET)
            value = self.parse_value()
            params[name_tok[1]] = value

            # Consume optional trailing comma
            tok = self.peek()
            if tok and tok[0] == TOKEN_COMMA:
                self.consume()
        return params

    def parse_value(self):
        """Parse a value and return as a typed tuple."""
        tok = self.peek()
        if tok is None:
            raise ValueError('Unexpected end of input, expected value')

        # Single-quoted string
        if tok[0] == TOKEN_SINGLE_STRING:
            self.consume()
            # Strip surrounding quotes to get inner text
            inner = tok[1][1:-1]
            return ('single', inner)

        # Double-quoted string
        if tok[0] == TOKEN_DOUBLE_STRING:
            self.consume()
            inner = tok[1][1:-1]
            return ('double', inner)

        # Number
        if tok[0] == TOKEN_NUMBER:
            self.consume()
            return ('number', tok[1])

        # Hash literal
        if tok[0] == TOKEN_LBRACE:
            return self.parse_hash()

        # Array literal
        if tok[0] == TOKEN_LBRACKET:
            return self.parse_array()

        # Bare identifiers: true, false, undef, or other idents
        if tok[0] == TOKEN_IDENT:
            self.consume()
            if tok[1] == 'undef':
                return ('undef', None)
            if tok[1] in ('true', 'false'):
                return ('bare', tok[1])
            # Other bare identifiers (e.g. resource references)
            return ('bare', tok[1])

        raise ValueError(
            'Unexpected token {} ({!r}) at position {}'.format(
                tok[0], tok[1], self.pos,
            )
        )

    def parse_hash(self):
        """Parse a hash: '{' (hash_entry (',' hash_entry)* ','?)? '}'

        Returns ('hash', OrderedDict_of_typed_key_value_pairs).
        """
        self.consume(TOKEN_LBRACE)
        entries = OrderedDict()

        while True:
            tok = self.peek()
            if tok is None or tok[0] == TOKEN_RBRACE:
                break

            # Hash key can be a string or a bare identifier
            key = self.parse_hash_key()
            self.consume(TOKEN_HASHROCKET)
            value = self.parse_value()
            entries[key] = value

            # Consume optional trailing comma
            tok = self.peek()
            if tok and tok[0] == TOKEN_COMMA:
                self.consume()

        self.consume(TOKEN_RBRACE)
        return ('hash', entries)

    def parse_hash_key(self):
        """Parse a hash key — returns a typed tuple like parse_value."""
        tok = self.peek()
        if tok and tok[0] == TOKEN_SINGLE_STRING:
            self.consume()
            inner = tok[1][1:-1]
            return ('single', inner)
        if tok and tok[0] == TOKEN_DOUBLE_STRING:
            self.consume()
            inner = tok[1][1:-1]
            return ('double', inner)
        if tok and tok[0] == TOKEN_IDENT:
            self.consume()
            return ('bare', tok[1])
        if tok and tok[0] == TOKEN_NUMBER:
            self.consume()
            return ('number', tok[1])
        raise ValueError(
            'Expected hash key, got {} ({!r}) at token {}'.format(
                tok[0] if tok else 'EOF',
                tok[1] if tok else '',
                self.pos,
            )
        )

    def parse_array(self):
        """Parse an array: '[' (value (',' value)* ','?)? ']'

        Returns ('array', list_of_typed_values).
        """
        self.consume(TOKEN_LBRACKET)
        items = []

        while True:
            tok = self.peek()
            if tok is None or tok[0] == TOKEN_RBRACKET:
                break
            items.append(self.parse_value())

            # Consume optional trailing comma
            tok = self.peek()
            if tok and tok[0] == TOKEN_COMMA:
                self.consume()

        self.consume(TOKEN_RBRACKET)
        return ('array', items)


# =============================================================================
# YAML Writer (Custom)
# =============================================================================

# Characters that require quoting in YAML keys/values
_YAML_UNSAFE_RE = re.compile(r'[:\{\}\[\],&\*\?|>!\#%@`\s]')


def _needs_quoting(text):
    """Check if a string needs to be quoted in YAML output.

    Returns True for strings containing special YAML characters,
    empty strings, or strings that could be misinterpreted as
    YAML booleans/nulls/numbers.
    """
    if not text:
        return True
    # Strings that YAML might misinterpret
    if text.lower() in ('true', 'false', 'yes', 'no', 'on', 'off',
                         'null', 'none', '~'):
        return True
    # Starts with special character
    if text[0] in ('"', "'", '-', '{', '[', '>', '|', '!', '&', '*',
                    '?', '%', '@', '`', ','):
        return True
    # Contains unsafe characters
    if _YAML_UNSAFE_RE.search(text):
        return True
    # Looks like a number
    try:
        float(text)
        return True
    except ValueError:
        pass
    return False


def _format_key(typed_key):
    """Format a hash key for YAML output, preserving quoting when needed."""
    kind, value = typed_key[0], typed_key[1]

    if kind == 'single':
        # Only quote if the value needs it for YAML safety
        if _needs_quoting(value):
            return "'{}'".format(value)
        return value
    if kind == 'double':
        if _needs_quoting(value):
            return '"{}"'.format(value)
        return value
    if kind == 'bare':
        if _needs_quoting(value):
            return "'{}'".format(value)
        return value
    if kind == 'number':
        return value
    # Fallback
    return str(value)


def _format_scalar(typed_value):
    """Format a scalar value for YAML output, preserving quoting."""
    kind, value = typed_value[0], typed_value[1]

    if kind == 'single':
        return "'{}'".format(value)
    if kind == 'double':
        return '"{}"'.format(value)
    if kind == 'bare':
        # Booleans and identifiers stay unquoted
        return value
    if kind == 'number':
        return value
    if kind == 'undef':
        return 'null'
    return str(value)


def _write_value(typed_value, indent, lines):
    """Write a typed value to the YAML output lines at the given indent level.

    For scalars, appends to the last line.
    For hashes/arrays, creates new indented lines.
    """
    kind = typed_value[0]

    if kind == 'hash':
        entries = typed_value[1]
        if not entries:
            # Empty hash — inline
            lines[-1] += ' {}'
            return
        for key, val in entries.items():
            prefix = '  ' * indent
            lines.append('{}{}:'.format(prefix, _format_key(key)))
            if val[0] in ('hash', 'array'):
                _write_value(val, indent + 1, lines)
            else:
                lines[-1] += ' {}'.format(_format_scalar(val))

    elif kind == 'array':
        items = typed_value[1]
        if not items:
            # Empty array — inline
            lines[-1] += ' []'
            return
        for item in items:
            prefix = '  ' * indent
            if item[0] in ('hash', 'array'):
                # Complex array element — use block style
                lines.append('{}-'.format(prefix))
                _write_value(item, indent + 1, lines)
            else:
                lines.append('{}- {}'.format(prefix, _format_scalar(item)))

    else:
        # Scalar value — append to current line
        lines[-1] += ' {}'.format(_format_scalar(typed_value))


def declarations_to_yaml(declarations):
    """Convert parsed declarations to YAML string.

    Groups multiple resources of the same type under a single top-level key.
    For class declarations, each parameter becomes a separate top-level key
    prefixed with the class name (e.g. opn::devices).
    """
    lines = []

    # Group declarations by type name to merge resources of the same type
    grouped = OrderedDict()
    class_decls = []

    for type_name, title, params in declarations:
        if type_name == 'class':
            class_decls.append((title, params))
        else:
            if type_name not in grouped:
                grouped[type_name] = OrderedDict()
            grouped[type_name][title] = params

    # Write class declarations first — each param becomes a top-level key
    for class_name, params in class_decls:
        for param_name, value in params.items():
            top_key = '{}::{}'.format(class_name, param_name)
            lines.append('{}:'.format(top_key))
            if value[0] in ('hash', 'array'):
                _write_value(value, 1, lines)
            else:
                lines[-1] += ' {}'.format(_format_scalar(value))

    # Write resource declarations — grouped by type
    for type_name, resources in grouped.items():
        lines.append('{}:'.format(type_name))
        for title, params in resources.items():
            # Title as second-level key
            title_key = title
            if _needs_quoting(title):
                title_key = "'{}'".format(title)
            lines.append('  {}:'.format(title_key))
            for param_name, value in params.items():
                lines.append('    {}:'.format(param_name))
                if value[0] in ('hash', 'array'):
                    _write_value(value, 3, lines)
                else:
                    lines[-1] += ' {}'.format(_format_scalar(value))

    # Join with newlines and add trailing newline
    return '\n'.join(lines) + '\n' if lines else ''


# =============================================================================
# CLI
# =============================================================================

def main():
    """Main entry point — parse CLI args, read input, convert, write output."""
    if len(sys.argv) < 2:
        print(
            'Usage: python3 puppet2hiera.py <input_file> [output_file]',
            file=sys.stderr,
        )
        print(
            '       python3 puppet2hiera.py -  # read from stdin',
            file=sys.stderr,
        )
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None

    # Read input
    if input_path == '-':
        source = sys.stdin.read()
    else:
        with open(input_path, 'r') as f:
            source = f.read()

    # Tokenize → Parse → Convert
    tokens = tokenize(source)
    parser = Parser(tokens)
    declarations = parser.parse()
    yaml_output = declarations_to_yaml(declarations)

    # Write output
    if output_path:
        with open(output_path, 'w') as f:
            f.write(yaml_output)
    else:
        sys.stdout.write(yaml_output)


if __name__ == '__main__':
    main()
