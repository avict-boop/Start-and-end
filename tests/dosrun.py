"""Мини-DOS для тестов: запускает MZ EXE в эмуляторе и возвращает вывод на экран (int 21h, AH=02/09)."""

from unicorn import UC_ARCH_X86, UC_HOOK_INTR, UC_MODE_16, Uc
from unicorn.x86_const import (UC_X86_REG_AX, UC_X86_REG_CS, UC_X86_REG_DS, UC_X86_REG_DX, UC_X86_REG_ES,
                               UC_X86_REG_IP, UC_X86_REG_SP, UC_X86_REG_SS)

from dosrip.exe.mz import parse_mz


def run_exe(data: bytes, encoding="cp437", max_insn=1_000_000) -> str:
    mz = parse_mz(data)
    load, psp = 0x1000, 0x0FF0
    uc = Uc(UC_ARCH_X86, UC_MODE_16)
    uc.mem_map(0, 0x110000)
    uc.mem_write(psp * 16, b"\xcd\x20" + (0xA000).to_bytes(2, "little"))
    img = bytearray(mz.image)
    for lin in mz.reloc_linear():
        v = (int.from_bytes(img[lin:lin + 2], "little") + load) & 0xFFFF
        img[lin:lin + 2] = v.to_bytes(2, "little")
    uc.mem_write(load * 16, bytes(img))
    out = bytearray()

    def intr(uc, n, _):
        ah = uc.reg_read(UC_X86_REG_AX) >> 8
        if n == 0x21 and ah == 0x09:
            p = uc.reg_read(UC_X86_REG_DS) * 16 + uc.reg_read(UC_X86_REG_DX)
            while (c := uc.mem_read(p, 1)[0]) != ord("$"):
                out.append(c)
                p += 1
        elif n == 0x21 and ah == 0x02:
            out.append(uc.reg_read(UC_X86_REG_DX) & 0xFF)
        elif (n == 0x21 and ah == 0x4C) or n == 0x20:
            uc.emu_stop()

    uc.hook_add(UC_HOOK_INTR, intr)
    uc.reg_write(UC_X86_REG_CS, load + mz.cs)
    uc.reg_write(UC_X86_REG_IP, mz.ip)
    uc.reg_write(UC_X86_REG_SS, load + mz.ss)
    uc.reg_write(UC_X86_REG_SP, mz.sp)
    uc.reg_write(UC_X86_REG_DS, psp)
    uc.reg_write(UC_X86_REG_ES, psp)
    uc.emu_start((load + mz.cs) * 16 + mz.ip, 0x10FFFF, count=max_insn)
    return out.decode(encoding)
