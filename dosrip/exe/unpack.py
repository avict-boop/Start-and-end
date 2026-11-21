"""Универсальный распаковщик сжатых DOS EXE (LZEXE, PKLITE, EXEPACK, DIET, ...).

Идея: распаковщик внутри EXE сам умеет себя распаковывать. Мы запускаем его в
эмуляторе процессора 8086 (Unicorn), ловим момент дальнего перехода в только что
распакованный код (оригинальная точка входа, OEP) и снимаем дамп памяти.

Таблицу релокаций восстанавливаем так: программа загружается два раза по разным
сегментам. Слова, которые отличаются ровно на разницу сегментов, — это релокации.
"""

from dataclasses import dataclass

from unicorn import UC_ARCH_X86, UC_HOOK_BLOCK, UC_HOOK_INTR, UC_MODE_16, Uc, UcError
from unicorn.x86_const import (
    UC_X86_REG_AX, UC_X86_REG_BX, UC_X86_REG_CS, UC_X86_REG_DS, UC_X86_REG_EFLAGS, UC_X86_REG_ES,
    UC_X86_REG_IP, UC_X86_REG_SP, UC_X86_REG_SS,
)

from .mz import MZ, build_mz, parse_mz

MEM_TOP = 0xA0000
LOAD_A, LOAD_B = 0x1000, 0x1234


@dataclass
class UnpackResult:
    exe: bytes
    oep: tuple  # (cs, ip) относительно начала образа
    relocs: int
    steps: int  # сколько слоёв упаковки снято


class _Stop(Exception):
    pass


def _run(mz: MZ, load_seg: int, max_insn: int):
    psp = load_seg - 0x10
    image = mz.image
    if load_seg * 16 + len(image) + mz.minalloc * 16 > MEM_TOP:
        raise ValueError("образ слишком велик для эмуляции")
    uc = Uc(UC_ARCH_X86, UC_MODE_16)
    uc.mem_map(0, 0x110000)
    # PSP
    psp_bytes = bytearray(256)
    psp_bytes[0:2] = b"\xcd\x20"
    psp_bytes[2:4] = (MEM_TOP >> 4).to_bytes(2, "little")
    psp_bytes[0x80], psp_bytes[0x81] = 0, 0x0D
    uc.mem_write(psp * 16, bytes(psp_bytes))
    # образ с применёнными релокациями — как делает DOS
    img = bytearray(image)
    for lin in mz.reloc_linear():
        if lin + 2 <= len(img):
            v = (int.from_bytes(img[lin: lin + 2], "little") + load_seg) & 0xFFFF
            img[lin: lin + 2] = v.to_bytes(2, "little")
    base = load_seg * 16
    uc.mem_write(base, bytes(img))
    initial = bytes(img)

    stub_lin = base + mz.cs * 16
    state = {"prev_cs": (mz.cs + load_seg) & 0xFFFF, "oep": None, "exit": False}

    def on_intr(uc, intno, _ud):
        ah = uc.reg_read(UC_X86_REG_AX) >> 8
        flags = uc.reg_read(UC_X86_REG_EFLAGS) & ~1
        if intno == 0x21:
            if ah in (0x4C, 0x00, 0x31):
                state["exit"] = True
                uc.emu_stop()
                return
            if ah == 0x30:
                uc.reg_write(UC_X86_REG_AX, 0x0005)
            elif ah == 0x48:  # выделение памяти — отказ
                uc.reg_write(UC_X86_REG_AX, 8)
                uc.reg_write(UC_X86_REG_BX, 0)
                flags |= 1
            elif ah == 0x35:
                uc.reg_write(UC_X86_REG_ES, 0)
                uc.reg_write(UC_X86_REG_BX, 0)
            elif ah == 0x62:
                uc.reg_write(UC_X86_REG_BX, psp)
        elif intno == 0x20:
            state["exit"] = True
            uc.emu_stop()
            return
        uc.reg_write(UC_X86_REG_EFLAGS, flags)

    def on_block(uc, address, _size, _ud):
        cs = uc.reg_read(UC_X86_REG_CS)
        if cs == state["prev_cs"]:
            return
        state["prev_cs"] = cs
        if base <= address < stub_lin:
            off = address - base
            current = bytes(uc.mem_read(address, 16))
            if current != initial[off: off + 16]:
                state["oep"] = (cs, uc.reg_read(UC_X86_REG_IP), uc.reg_read(UC_X86_REG_SS), uc.reg_read(UC_X86_REG_SP))
                uc.emu_stop()

    uc.hook_add(UC_HOOK_INTR, on_intr)
    uc.hook_add(UC_HOOK_BLOCK, on_block)
    uc.reg_write(UC_X86_REG_CS, (load_seg + mz.cs) & 0xFFFF)
    uc.reg_write(UC_X86_REG_IP, mz.ip)
    uc.reg_write(UC_X86_REG_SS, (load_seg + mz.ss) & 0xFFFF)
    uc.reg_write(UC_X86_REG_SP, mz.sp)
    uc.reg_write(UC_X86_REG_DS, psp)
    uc.reg_write(UC_X86_REG_ES, psp)
    uc.reg_write(UC_X86_REG_AX, 0)
    try:
        uc.emu_start(base + mz.entry_offset, 0x10FFFF, count=max_insn)
    except UcError:
        pass
    if not state["oep"]:
        return None
    memory = bytes(uc.mem_read(base, MEM_TOP - base))
    return state["oep"], memory, initial


def _dump_end(memory: bytes, initial: bytes, stub_off: int, stub: bytes) -> int:
    """Где кончается распакованная программа."""
    # если распаковщик скопировал себя выше — отрезаем копию
    probe = stub[:32]
    search_from = stub_off + 1
    copy_at = memory.find(probe, search_from) if len(probe) >= 16 else -1
    limit = copy_at if copy_at != -1 else len(memory)
    # заглушка на своём месте почти не изменилась -> это не часть программы
    region = memory[stub_off: stub_off + len(stub)]
    changed = sum(1 for a, b in zip(region, stub) if a != b)
    if stub and changed < max(8, len(stub) // 20):
        limit = min(limit, stub_off)
    end = 0
    init_padded = initial + bytes(max(0, limit - len(initial)))
    for i in range(limit - 1, -1, -1):
        if memory[i] != init_padded[i] and memory[i] != 0:
            end = i + 1
            break
    if end == 0:
        end = min(limit, len(initial))
    return (end + 15) & ~15


def unpack_once(mz: MZ, max_insn: int = 60_000_000):
    a = _run(mz, LOAD_A, max_insn)
    if not a:
        return None
    b = _run(mz, LOAD_B, max_insn)
    if not b:
        return None
    (cs_a, ip_a, ss_a, sp_a), mem_a, init_a = a
    (cs_b, ip_b, ss_b, sp_b), mem_b, _ = b
    if (cs_a - LOAD_A) & 0xFFFF != (cs_b - LOAD_B) & 0xFFFF or ip_a != ip_b:
        return None
    stub_off = mz.cs * 16
    stub = init_a[stub_off:]
    end = _dump_end(mem_a, init_a, stub_off, stub)
    end = max(end, ((cs_a - LOAD_A) & 0xFFFF) * 16 + ip_a + 1)
    end = min(end, len(mem_a))
    image = bytearray(mem_a[:end])
    delta = LOAD_B - LOAD_A
    relocs = []
    i = 0
    while i < end - 1:
        if mem_a[i] != mem_b[i] or mem_a[i + 1] != mem_b[i + 1]:
            wa = int.from_bytes(mem_a[i: i + 2], "little")
            wb = int.from_bytes(mem_b[i: i + 2], "little")
            if (wb - wa) & 0xFFFF == delta:
                relocs.append(i)
                image[i: i + 2] = ((wa - LOAD_A) & 0xFFFF).to_bytes(2, "little")
                i += 2
                continue
        i += 1
    cs_rel, ss_rel = (cs_a - LOAD_A) & 0xFFFF, (ss_a - LOAD_A) & 0xFFFF
    paras_needed = (len(mz.image) + 15) // 16 + mz.minalloc
    paras_img = (len(image) + 15) // 16
    stack_top = (ss_rel * 16 + sp_a + 15) // 16
    minalloc = max(0, paras_needed - paras_img, stack_top - paras_img)
    exe = build_mz(bytes(image), relocs, cs_rel, ip_a, ss_rel, sp_a, minalloc=min(minalloc, 0xFFFF))
    return exe, (cs_rel, ip_a), len(relocs)


def unpack(data: bytes, max_layers: int = 4, max_insn: int = 60_000_000):
    """Снимает все слои упаковки. -> UnpackResult или None, если не получилось."""
    mz = parse_mz(data)
    if not mz or mz.new_exe:
        return None
    result, steps, overlay = None, 0, mz.overlay
    while steps < max_layers:
        try:
            r = unpack_once(mz, max_insn if mz.packer else min(max_insn, 5_000_000))
        except (ValueError, UcError):
            r = None
        if not r:
            break
        exe, oep, nrel = r
        steps += 1
        result = UnpackResult(exe=exe + overlay, oep=oep, relocs=nrel, steps=steps)
        mz2 = parse_mz(exe)
        if not mz2 or not mz2.packer:
            break
        mz = mz2
    return result
