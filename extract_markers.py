#!/usr/bin/env python3
"""Extract exact marker-tag literal strings from source files as codepoints."""
import ast
import io
import token as tokenmod
import tokenize

FILES = [
    '/root/wrapper/nvidia-python/src/anthropic_compat.py',
    '/root/wrapper/common/translations/shared.py',
]

KEYWORDS = ('think', 'Think', 'THINK', 'reason', 'Reason')


def main():
    for path in FILES:
        print(f'== {path} ==')
        src = open(path, 'r', encoding='utf-8', errors='replace').read()
        for i, line in enumerate(src.splitlines(), 1):
            if 'find(' in line or 'startswith(' in line or 'endswith(' in line or 'replace(' in line:
                if any(k in line for k in KEYWORDS) or 'in text' in line or 'token' in line.lower():
                    # extract string literals in this line
                    try:
                        for tok in tokenize.generate_tokens(io.StringIO(line).readline):
                            if tok.type == tokenmod.STRING:
                                try:
                                    val = ast.literal_eval(tok.string)
                                except Exception:
                                    continue
                                if any(k in str(val) for k in KEYWORDS) or 'think' in str(val).lower():
                                    cps = ' '.join(f'U+{ord(c):04X}' for c in str(val)[:20])
                                    print(f'L{i}: {tok.string!r} -> ascii: {ascii(str(val))[:60]} | cps: {cps}')
                    except Exception:
                        pass


if __name__ == '__main__':
    main()
