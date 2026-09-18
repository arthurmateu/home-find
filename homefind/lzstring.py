"""Minimal LZ-String decompressFromBase64 (port of pieroxy/lz-string), stdlib only.

Immowelt ships its search results as an LZ-String-compressed JSON blob.
"""

_KEY = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
_REV = {c: i for i, c in enumerate(_KEY)}


def decompress_from_base64(s: str) -> str | None:
    if not s:
        return ""
    return _decompress(len(s), 32, lambda i: _REV[s[i]])


def _decompress(length, reset_value, get_next):
    dictionary = {0: 0, 1: 1, 2: 2}
    enlarge_in, dict_size, num_bits = 4, 4, 3
    result = []
    data_val, data_pos, data_idx = get_next(0), reset_value, 1

    def read_bits(n):
        nonlocal data_val, data_pos, data_idx
        bits, power, max_power = 0, 1, 1 << n
        while power != max_power:
            resb = data_val & data_pos
            data_pos >>= 1
            if data_pos == 0:
                data_pos = reset_value
                data_val = get_next(data_idx) if data_idx < length else 0
                data_idx += 1
            bits |= (1 if resb > 0 else 0) * power
            power <<= 1
        return bits

    nxt = read_bits(2)
    if nxt == 0:
        c = chr(read_bits(8))
    elif nxt == 1:
        c = chr(read_bits(16))
    else:
        return ""
    dictionary[3] = c
    w = c
    result.append(c)

    while True:
        if data_idx > length:
            return ""
        c = read_bits(num_bits)
        if c == 0:
            dictionary[dict_size] = chr(read_bits(8))
            dict_size += 1
            c = dict_size - 1
            enlarge_in -= 1
        elif c == 1:
            dictionary[dict_size] = chr(read_bits(16))
            dict_size += 1
            c = dict_size - 1
            enlarge_in -= 1
        elif c == 2:
            return "".join(result)

        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1

        if c in dictionary:
            entry = dictionary[c]
        elif c == dict_size:
            entry = w + w[0]
        else:
            return None
        result.append(entry)

        dictionary[dict_size] = w + entry[0]
        dict_size += 1
        enlarge_in -= 1
        w = entry

        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
