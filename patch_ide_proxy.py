#!/usr/bin/env python3
"""Patch IdeProxyAuthenticator so a null stored password does not open a dialog.

The original bytecode asks for prompted authentication when any of these is null:
credentials, credentials.userName, credentials.password.  This patch removes only
the password null check.  A missing password is subsequently converted to an empty
char array by the original method.

Usage:
    python patch_ide_proxy.py path/to/intellij.platform.ide.jar
    python patch_ide_proxy.py input.jar --output patched.jar

Without --output the JAR is replaced atomically and a .bak copy is created.
Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile


CLASS_NAME = "com/intellij/util/net/IdeProxyAuthenticator.class"
METHOD_NAME = "requestPasswordAuthenticationInstance"
METHOD_DESCRIPTOR = (
    "(Ljava/lang/String;Ljava/net/InetAddress;ILjava/lang/String;"
    "Ljava/lang/String;Ljava/lang/String;Ljava/net/URL;"
    "Ljava/net/Authenticator$RequestorType;)Ljava/net/PasswordAuthentication;"
)
CREDENTIALS_CLASS = "com/intellij/credentialStore/Credentials"
GET_PASSWORD = "getPassword"
GET_PASSWORD_DESCRIPTOR = "()Lcom/intellij/credentialStore/OneTimeString;"
PROXY_AUTHENTICATION_CLASS = "com/intellij/util/net/ProxyAuthentication"
GET_KNOWN_AUTHENTICATION = "getKnownAuthentication"
GET_KNOWN_AUTHENTICATION_DESCRIPTOR = (
    "(Ljava/lang/String;I)Lcom/intellij/credentialStore/Credentials;"
)


class PatchError(RuntimeError):
    pass


class Reader:
    def __init__(self, data: bytes, offset: int = 0):
        self.data = data
        self.offset = offset

    def take(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.data):
            raise PatchError("Unexpected end of class file")
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def u1(self) -> int:
        return self.take(1)[0]

    def u2(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def u4(self) -> int:
        return struct.unpack(">I", self.take(4))[0]


# Number of operand bytes for the fixed-length JVM instructions.
_OPERAND_LENGTHS = [0] * 256
for _opcode, _length in {
    0x10: 1, 0x11: 2, 0x12: 1, 0x13: 2, 0x14: 2,
    0x15: 1, 0x16: 1, 0x17: 1, 0x18: 1, 0x19: 1,
    0x36: 1, 0x37: 1, 0x38: 1, 0x39: 1, 0x3A: 1,
    0x84: 2,
    0x99: 2, 0x9A: 2, 0x9B: 2, 0x9C: 2, 0x9D: 2, 0x9E: 2,
    0x9F: 2, 0xA0: 2, 0xA1: 2, 0xA2: 2, 0xA3: 2, 0xA4: 2,
    0xA5: 2, 0xA6: 2, 0xA7: 2, 0xA8: 2, 0xA9: 1,
    0xB2: 2, 0xB3: 2, 0xB4: 2, 0xB5: 2, 0xB6: 2, 0xB7: 2,
    0xB8: 2, 0xB9: 4, 0xBA: 4, 0xBB: 2, 0xBC: 1, 0xBD: 2,
    0xC0: 2, 0xC1: 2, 0xC5: 3, 0xC6: 2, 0xC7: 2,
    0xC8: 4, 0xC9: 4,
}.items():
    _OPERAND_LENGTHS[_opcode] = _length


def _skip_attributes(reader: Reader) -> None:
    for _ in range(reader.u2()):
        reader.u2()  # attribute_name_index
        reader.take(reader.u4())


def _parse_constant_pool(reader: Reader) -> list[tuple | None]:
    count = reader.u2()
    pool: list[tuple | None] = [None] * count
    index = 1
    while index < count:
        tag = reader.u1()
        if tag == 1:  # Utf8 (modified UTF-8; these identifiers are ASCII)
            pool[index] = (tag, reader.take(reader.u2()).decode("utf-8"))
        elif tag in (3, 4):  # Integer, Float
            pool[index] = (tag, reader.take(4))
        elif tag in (5, 6):  # Long, Double occupy two slots
            pool[index] = (tag, reader.take(8))
            index += 1
        elif tag in (7, 8, 16, 19, 20):  # Class, String, MethodType, Module, Package
            pool[index] = (tag, reader.u2())
        elif tag in (9, 10, 11, 12, 17, 18):
            # Field/Method/InterfaceMethod ref, NameAndType, Dynamic, InvokeDynamic
            pool[index] = (tag, reader.u2(), reader.u2())
        elif tag == 15:  # MethodHandle
            pool[index] = (tag, reader.u1(), reader.u2())
        else:
            raise PatchError(f"Unsupported constant pool tag {tag} at index {index}")
        index += 1
    return pool


def _utf8(pool: list[tuple | None], index: int) -> str:
    entry = pool[index]
    if entry is None or entry[0] != 1:
        raise PatchError(f"Constant pool entry #{index} is not Utf8")
    return entry[1]


def _method_reference(pool: list[tuple | None], index: int) -> tuple[str, str, str]:
    entry = pool[index]
    if entry is None or entry[0] not in (10, 11):
        raise PatchError(f"Constant pool entry #{index} is not a method reference")
    class_entry = pool[entry[1]]
    name_and_type = pool[entry[2]]
    if class_entry is None or class_entry[0] != 7 or name_and_type is None or name_and_type[0] != 12:
        raise PatchError(f"Malformed method reference #{index}")
    return (
        _utf8(pool, class_entry[1]),
        _utf8(pool, name_and_type[1]),
        _utf8(pool, name_and_type[2]),
    )


def _instructions(code: bytes):
    """Yield (offset, opcode, operand offset, next offset), validating all offsets."""
    offset = 0
    while offset < len(code):
        start = offset
        opcode = code[offset]
        offset += 1
        operand_offset = offset
        if opcode == 0xAA:  # tableswitch
            offset += (-offset) % 4
            if offset + 12 > len(code):
                raise PatchError("Truncated tableswitch")
            low, high = struct.unpack_from(">ii", code, offset + 4)
            if high < low:
                raise PatchError("Invalid tableswitch")
            offset += 12 + (high - low + 1) * 4
        elif opcode == 0xAB:  # lookupswitch
            offset += (-offset) % 4
            if offset + 8 > len(code):
                raise PatchError("Truncated lookupswitch")
            pairs = struct.unpack_from(">i", code, offset + 4)[0]
            if pairs < 0:
                raise PatchError("Invalid lookupswitch")
            offset += 8 + pairs * 8
        elif opcode == 0xC4:  # wide
            if offset >= len(code):
                raise PatchError("Truncated wide instruction")
            offset += 4 if code[offset] == 0x84 else 2
        else:
            offset += _OPERAND_LENGTHS[opcode]
        if offset > len(code):
            raise PatchError(f"Truncated bytecode instruction at {start}")
        yield start, opcode, operand_offset, offset


def _verification_type_size(data: bytes, offset: int) -> int:
    tag = data[offset]
    if tag > 8:
        raise PatchError(f"Invalid StackMapTable verification type {tag}")
    return 3 if tag in (7, 8) else 1


def _insert_unreachable_frame(
    patched: bytearray,
    pool: list[tuple | None],
    code_attribute_offset: int,
    code_reader: Reader,
    unreachable_bci: int,
) -> None:
    """Move an append_frame to NOP padding following an unconditional branch."""
    code_reader.take(code_reader.u2() * 8)  # exception_table
    stack_map = None
    for _ in range(code_reader.u2()):
        name = _utf8(pool, code_reader.u2())
        length_offset = code_reader.offset
        length = code_reader.u4()
        data_offset = code_reader.offset
        if name == "StackMapTable":
            stack_map = (length_offset, data_offset, length)
        code_reader.take(length)
    if stack_map is None:
        raise PatchError("StackMapTable is missing; cannot safely add an unconditional branch")

    length_offset, table_offset, table_length = stack_map
    table_end = table_offset + table_length
    entries_offset = table_offset + 2
    count = struct.unpack_from(">H", patched, table_offset)[0]
    entries = []
    offset = entries_offset
    previous_bci = -1
    for _ in range(count):
        entry_offset = offset
        tag = patched[offset]
        offset += 1
        if tag <= 63:
            delta = tag
        elif tag <= 127:
            delta = tag - 64
            offset += _verification_type_size(patched, offset)
        elif tag == 247:
            delta = struct.unpack_from(">H", patched, offset)[0]
            offset += 2 + _verification_type_size(patched, offset + 2)
        elif 248 <= tag <= 251:
            delta = struct.unpack_from(">H", patched, offset)[0]
            offset += 2
        elif 252 <= tag <= 254:
            delta = struct.unpack_from(">H", patched, offset)[0]
            offset += 2
            for _ in range(tag - 251):
                offset += _verification_type_size(patched, offset)
        elif tag == 255:
            delta = struct.unpack_from(">H", patched, offset)[0]
            offset += 2
            locals_count = struct.unpack_from(">H", patched, offset)[0]
            offset += 2
            for _ in range(locals_count):
                offset += _verification_type_size(patched, offset)
            stack_count = struct.unpack_from(">H", patched, offset)[0]
            offset += 2
            for _ in range(stack_count):
                offset += _verification_type_size(patched, offset)
        else:
            raise PatchError(f"Unsupported StackMapTable frame tag {tag}")
        bci = previous_bci + delta + 1
        entries.append((bci, entry_offset, tag, delta))
        previous_bci = bci
    if offset != table_end:
        raise PatchError("Malformed StackMapTable")

    next_index = next((i for i, entry in enumerate(entries) if entry[0] > unreachable_bci), None)
    if next_index is None or any(entry[0] == unreachable_bci for entry in entries):
        raise PatchError("Unexpected StackMapTable layout for inserted frame")
    previous_bci = entries[next_index - 1][0] if next_index else -1
    next_bci, next_offset, next_tag, _ = entries[next_index]
    delta = unreachable_bci - previous_bci - 1
    next_delta = next_bci - unreachable_bci - 1
    if not 0 <= delta <= 63 or not 252 <= next_tag <= 254:
        raise PatchError("Expected an adjacent append_frame for hostName and credentials")
    next_end = entries[next_index + 1][1] if next_index + 1 < len(entries) else table_end

    # Move the existing append_frame (hostName and credentials) to the start of
    # the unreachable padding.  Its old location then needs no local changes.
    moved_frame = bytearray(patched[next_offset:next_end])
    struct.pack_into(">H", moved_frame, 1, delta)
    same_frame = b"\xfb" + struct.pack(">H", next_delta)  # same_frame_extended
    growth = len(moved_frame) + len(same_frame) - (next_end - next_offset)
    struct.pack_into(">I", patched, length_offset, table_length + growth)
    struct.pack_into(">I", patched, code_attribute_offset - 4,
                     struct.unpack_from(">I", patched, code_attribute_offset - 4)[0] + growth)
    struct.pack_into(">H", patched, table_offset, count + 1)
    patched[next_offset:next_end] = moved_frame + same_frame


def patch_class(data: bytes) -> tuple[bytes, str]:
    reader = Reader(data)
    if reader.u4() != 0xCAFEBABE:
        raise PatchError("Target entry is not a Java class file")
    reader.u2()  # minor
    reader.u2()  # major
    pool = _parse_constant_pool(reader)
    reader.take(6)  # access_flags, this_class, super_class
    reader.take(reader.u2() * 2)  # interfaces
    for _ in range(reader.u2()):  # fields
        reader.take(6)
        _skip_attributes(reader)

    result: bytes | None = None
    status: str | None = None
    for _ in range(reader.u2()):
        reader.u2()  # access_flags
        name = _utf8(pool, reader.u2())
        descriptor = _utf8(pool, reader.u2())
        attributes_count = reader.u2()
        for _ in range(attributes_count):
            attribute_name = _utf8(pool, reader.u2())
            attribute_length = reader.u4()
            attribute_offset = reader.offset
            if name != METHOD_NAME or descriptor != METHOD_DESCRIPTOR or attribute_name != "Code":
                reader.take(attribute_length)
                continue

            code_reader = Reader(data, attribute_offset)
            code_reader.u2()  # max_stack
            code_reader.u2()  # max_locals
            code_length = code_reader.u4()
            code_offset = code_reader.offset
            code = code_reader.take(code_length)
            instructions = list(_instructions(code))
            candidates = []
            fixed_candidates = []
            for index, instruction in enumerate(instructions[:-1]):
                start, opcode, operand_offset, next_offset = instruction
                if opcode == 0xA7 and index + 5 < len(instructions):
                    # Shape emitted by this patcher: goto + five NOP instructions.
                    if all(instructions[index + n][1] == 0 for n in range(1, 6)):
                        branch = struct.unpack_from(">h", code, operand_offset)[0]
                        fixed_candidates.append((start, start + branch, instructions[index + 5][3]))
                if opcode != 0xB6:  # invokevirtual
                    continue
                cp_index = struct.unpack_from(">H", code, operand_offset)[0]
                reference = _method_reference(pool, cp_index)
                if reference != (CREDENTIALS_CLASS, GET_PASSWORD, GET_PASSWORD_DESCRIPTOR):
                    continue
                next_start, next_opcode, next_operand, after_next = instructions[index + 1]
                if next_opcode not in (0xC7, 0xA7):  # original check or broken v1 patch
                    continue
                previous_start, previous_opcode, previous_operand, previous_end = instructions[index - 1]
                if previous_end != start or previous_opcode != 0x19 or code[previous_operand] != 11:
                    raise PatchError("Expected aload credentials immediately before getPassword")
                branch = struct.unpack_from(">h", code, next_operand)[0]
                candidates.append((previous_start, next_opcode, next_start + branch, after_next))

            known_candidates = []
            for index, instruction in enumerate(instructions[:-3]):
                start, opcode, operand_offset, _ = instruction
                if opcode != 0xB9:  # invokeinterface
                    continue
                cp_index = struct.unpack_from(">H", code, operand_offset)[0]
                reference = _method_reference(pool, cp_index)
                if reference != (
                    PROXY_AUTHENTICATION_CLASS,
                    GET_KNOWN_AUTHENTICATION,
                    GET_KNOWN_AUTHENTICATION_DESCRIPTOR,
                ):
                    continue
                store = instructions[index + 1]
                load = instructions[index + 2]
                check = instructions[index + 3]
                if (
                    store[1] == 0x3A and code[store[2]] == 11  # astore credentials
                    and load[1] == 0x19 and code[load[2]] == 11  # aload credentials
                    and check[1] == 0xC6  # ifnull
                ):
                    branch = struct.unpack_from(">h", code, check[2])[0]
                    known_candidates.append((check[0], check[0] + branch))
            if len(known_candidates) != 1:
                raise PatchError(
                    f"Expected exactly one getKnownAuthentication null check, found {len(known_candidates)}"
                )
            known_check_offset, known_check_target = known_candidates[0]

            if not candidates and len(fixed_candidates) == 1:
                _, target, continuation = fixed_candidates[0]
                if code[target:target + 3] == b"\x19\x0b\xc6" and b"\xb9" in code[continuation:target]:
                    target_index = next(i for i, item in enumerate(instructions) if item[0] == target)
                    null_check = instructions[target_index + 1]
                    null_return_target = null_check[0] + struct.unpack_from(">h", code, null_check[2])[0]
                    if known_check_target == null_return_target:
                        result, status = data, "already patched"
                    else:
                        patched = bytearray(data)
                        struct.pack_into(
                            ">h", patched, code_offset + known_check_offset + 1,
                            null_return_target - known_check_offset,
                        )
                        result, status = bytes(patched), "patched null credentials handling"
                    reader.take(attribute_length)
                    continue

            if len(candidates) != 1:
                raise PatchError(
                    f"Expected exactly one getPassword null check, found {len(candidates)}; "
                    "this IDE version needs a different patch"
                )
            sequence_start, old_opcode, target, continuation = candidates[0]
            # Remove the complete sequence:
            #   aload credentials; invokevirtual getPassword; ifnonnull credentialsReady
            # Replacing only ifnonnull with goto would leave OneTimeString on the
            # operand stack and contradict StackMapTable (VerifyError at the target).
            # A goto plus NOP padding keeps every subsequent bytecode offset intact.
            skipped = code[continuation:target]
            if not skipped or target > len(code) or code[target:target + 3] != b"\x19\x0b\xc6":
                raise PatchError("Unexpected branch structure around getPassword")
            if b"\xb9" not in skipped:
                raise PatchError("Prompted authentication call not found in expected branch")
            patched = bytearray(data)
            absolute_start = code_offset + sequence_start
            patched[absolute_start] = 0xA7  # goto
            struct.pack_into(">h", patched, absolute_start + 1, target - sequence_start)
            patched[absolute_start + 3:code_offset + continuation] = b"\x00" * (continuation - sequence_start - 3)
            _insert_unreachable_frame(
                patched, pool, attribute_offset, code_reader, sequence_start + 3
            )
            target_index = next(i for i, item in enumerate(instructions) if item[0] == target)
            null_check = instructions[target_index + 1]
            null_return_target = null_check[0] + struct.unpack_from(">h", code, null_check[2])[0]
            struct.pack_into(
                ">h", patched, code_offset + known_check_offset + 1,
                null_return_target - known_check_offset,
            )
            result = bytes(patched)
            status = "repaired broken v1 patch" if old_opcode == 0xA7 else "patched"
            reader.take(attribute_length)
        # Other methods are irrelevant.

    if result is None:
        raise PatchError(f"Method {METHOD_NAME}{METHOD_DESCRIPTOR} or its Code attribute was not found")
    return result, status


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verify_with_bundled_jvm(source: Path, destination: Path) -> None:
    ide_root = source.parent.parent
    candidates = (
        ide_root / "jbr" / "bin" / "java.exe",
        ide_root / "jbr" / "bin" / "java",
        ide_root / "jbr" / "Contents" / "Home" / "bin" / "java",
    )
    java = next((path for path in candidates if path.is_file()), None)
    if java is None:
        print("Warning: bundled JetBrains Runtime not found; JVM verification skipped", file=sys.stderr)
        return
    with tempfile.TemporaryDirectory(prefix="ide-proxy-verify-") as directory:
        verifier = Path(directory) / "VerifyIdeProxyAuthenticator.java"
        verifier.write_text(
            "public final class VerifyIdeProxyAuthenticator {\n"
            "  public static void main(String[] args) throws Exception {\n"
            "    Class.forName(\"com.intellij.util.net.IdeProxyAuthenticator\");\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        classpath = os.pathsep.join((str(destination), str(source.parent / "*")))
        process = subprocess.run(
            [str(java), "-Xverify:all", "-cp", classpath, str(verifier)],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if process.returncode != 0:
            details = (process.stderr or process.stdout).strip()
            raise PatchError(f"JVM bytecode verification failed; JAR was not installed:\n{details}")


def patch_jar(source: Path, destination: Path) -> str:
    try:
        with zipfile.ZipFile(source, "r") as zin:
            original_class = zin.read(CLASS_NAME)
            patched_class, status = patch_class(original_class)
            if status == "already patched" and zin.comment:
                status = "removed stale JetBrains IKV cache"
            if status == "already patched" and source.resolve() == destination.resolve():
                return status
            with zipfile.ZipFile(destination, "w") as zout:
                # JetBrains stores a private IKV central-directory cache in the
                # ZIP comment.  It contains absolute offsets and becomes stale as
                # soon as an entry changes size.  ImmutableZipFile then fails with
                # `newPosition > limit` before IDE startup.  Dropping the optional
                # cache makes the loader parse the valid standard central directory.
                zout.comment = b""
                for info in zin.infolist():
                    contents = patched_class if info.filename == CLASS_NAME else zin.read(info.filename)
                    zout.writestr(info, contents)
        with zipfile.ZipFile(destination, "r") as check:
            bad_entry = check.testzip()
            if bad_entry is not None:
                raise PatchError(f"ZIP integrity check failed for {bad_entry}")
            verified, _ = patch_class(check.read(CLASS_NAME))
            if verified != patched_class:
                raise PatchError("Patched class verification failed")
        _verify_with_bundled_jvm(source, destination)
        return status
    except KeyError as error:
        raise PatchError(f"{CLASS_NAME} was not found in {source}") from error
    except zipfile.BadZipFile as error:
        raise PatchError(f"Not a valid JAR/ZIP file: {source}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jar", type=Path, help="Path to intellij.platform.ide.jar")
    parser.add_argument("-o", "--output", type=Path, help="Write a patched copy instead of modifying the input")
    args = parser.parse_args()
    source = args.jar.expanduser().resolve()
    if not source.is_file():
        parser.error(f"JAR does not exist: {source}")
    if source.name != "intellij.platform.ide.jar":
        print(f"Warning: input filename is {source.name}, not intellij.platform.ide.jar", file=sys.stderr)

    try:
        if args.output:
            destination = args.output.expanduser().resolve()
            if destination == source:
                parser.error("--output must differ from the input JAR")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                parser.error(f"Output already exists: {destination}")
            try:
                status = patch_jar(source, destination)
            except Exception:
                destination.unlink(missing_ok=True)
                raise
        else:
            backup = source.with_suffix(source.suffix + ".bak")
            with tempfile.NamedTemporaryFile(
                prefix=source.name + ".", suffix=".tmp", dir=source.parent, delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
            try:
                status = patch_jar(source, temporary_path)
                changed = status != "already patched"
                if changed:
                    if status == "patched" and backup.exists():
                        raise PatchError(f"Backup already exists; refusing to overwrite it: {backup}")
                    if not backup.exists():
                        shutil.copy2(source, backup)
                    os.replace(temporary_path, source)
                    shutil.copystat(backup, source)
                else:
                    temporary_path.unlink()
                destination = source
            except Exception:
                temporary_path.unlink(missing_ok=True)
                raise

        print(f"Status: {status}")
        print(f"JAR: {destination}")
        if not args.output and status != "already patched":
            print(f"Backup: {backup}")
        print(f"SHA-256: {_sha256(destination.read_bytes())}")
        return 0
    except (PatchError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
