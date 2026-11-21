"""Дизассемблер 16-битного кода DOS (рекурсивный обход от точки входа)."""

from collections import deque

from capstone import CS_ARCH_X86, CS_MODE_16, CS_OP_IMM, Cs
from capstone import x86_const as X

DOS_INT21 = {
    0x00: "завершить программу", 0x01: "ввод символа с эхом", 0x02: "вывод символа", 0x06: "прямой ввод/вывод",
    0x07: "ввод символа без эха", 0x09: "вывод строки $", 0x0A: "ввод строки", 0x0B: "проверка клавиатуры",
    0x0E: "выбор диска", 0x19: "текущий диск", 0x1A: "установить DTA", 0x25: "установить вектор прерывания",
    0x2A: "дата", 0x2C: "время", 0x30: "версия DOS", 0x31: "остаться резидентом", 0x35: "получить вектор прерывания",
    0x3B: "сменить каталог", 0x3C: "создать файл", 0x3D: "открыть файл", 0x3E: "закрыть файл", 0x3F: "чтение файла",
    0x40: "запись в файл", 0x41: "удалить файл", 0x42: "позиционирование в файле", 0x43: "атрибуты файла",
    0x44: "IOCTL", 0x47: "текущий каталог", 0x48: "выделить память", 0x49: "освободить память",
    0x4A: "изменить размер блока памяти", 0x4B: "запустить программу/оверлей", 0x4C: "выход из программы",
    0x4E: "найти первый файл", 0x4F: "найти следующий файл", 0x56: "переименовать файл", 0x62: "получить PSP",
}
INT_NAMES = {
    0x10: "BIOS видео", 0x11: "BIOS оборудование", 0x13: "BIOS диск", 0x14: "BIOS COM-порт", 0x15: "BIOS система",
    0x16: "BIOS клавиатура", 0x1A: "BIOS часы", 0x21: "DOS", 0x2F: "мультиплекс (XMS/EMS/...)", 0x33: "мышь",
    0x67: "EMS",
}
INT10 = {0x00: "установить видеорежим", 0x02: "позиция курсора", 0x0E: "вывод символа (телетайп)",
         0x0F: "текущий видеорежим", 0x10: "палитра", 0x13: "вывод строки"}

_STOP = {"ret", "retf", "iret", "hlt"}


def _imm_ops(insn):
    return [op.imm for op in insn.operands if op.type == CS_OP_IMM]


class Disassembly:
    def __init__(self, image: bytes, entry_cs: int, entry_ip: int, relocs=(), max_insns: int = 400_000):
        self.image = image
        self.relocs = set(relocs)
        self.md = Cs(CS_ARCH_X86, CS_MODE_16)
        self.md.detail = True
        self.insns = {}  # линейный адрес -> (cs, insn)
        self.functions = {(entry_cs, entry_ip)}
        self.labels = set()
        self.comments = {}
        self.entry = (entry_cs, entry_ip)
        self.max_insns = max_insns
        self._walk()

    def _lin(self, cs, ip):
        return (cs * 16 + ip) & 0xFFFFF

    def _walk(self):
        queue = deque([self.entry])
        seen_starts = set()
        while queue and len(self.insns) < self.max_insns:
            cs, ip = queue.popleft()
            lin = self._lin(cs, ip)
            if lin in seen_starts or lin >= len(self.image):
                continue
            seen_starts.add(lin)
            last_ah = last_al = None
            while lin < len(self.image) and lin not in self.insns:
                chunk = self.image[lin: lin + 16]
                insn = next(self.md.disasm(chunk, ip), None)
                if insn is None:
                    break
                self.insns[lin] = (cs, insn)
                m = insn.mnemonic
                ops = _imm_ops(insn)
                # запоминаем mov ah, NN / mov ax, NNNN для комментариев к int
                if m == "mov" and insn.operands and insn.operands[0].type == X.X86_OP_REG and ops:
                    reg = insn.operands[0].reg
                    if reg == X.X86_REG_AH:
                        last_ah = ops[0] & 0xFF
                    elif reg == X.X86_REG_AX:
                        last_ah, last_al = (ops[0] >> 8) & 0xFF, ops[0] & 0xFF
                    elif reg == X.X86_REG_AL:
                        last_al = ops[0] & 0xFF
                if m == "int" and ops:
                    n = ops[0] & 0xFF
                    text = INT_NAMES.get(n, f"прерывание {n:02X}h")
                    if n == 0x21 and last_ah is not None:
                        text += f": AH={last_ah:02X}h {DOS_INT21.get(last_ah, '')}".rstrip()
                    elif n == 0x10 and last_ah is not None:
                        text += f": AH={last_ah:02X}h {INT10.get(last_ah, '')}".rstrip()
                        if last_ah == 0 and last_al is not None:
                            text += f" (режим {last_al:02X}h)"
                    self.comments[lin] = text
                    if n == 0x20 or (n == 0x21 and last_ah in (0x4C, 0x00)):
                        break

                nxt_ip = (ip + insn.size) & 0xFFFF
                if m in _STOP:
                    break
                if m in ("call", "lcall") or m.startswith("j") or m.startswith("loop") or m == "ljmp":
                    if m in ("lcall", "ljmp") and len(ops) == 2:
                        tgt = (ops[0], ops[1])
                    elif len(ops) == 1 and insn.operands[0].type == CS_OP_IMM:
                        tgt = (cs, ops[0] & 0xFFFF)
                    else:
                        tgt = None  # косвенный переход
                    if tgt:
                        if m in ("call", "lcall"):
                            self.functions.add(tgt)
                        else:
                            self.labels.add(self._lin(*tgt))
                        queue.append(tgt)
                    if m in ("jmp", "ljmp"):
                        break
                ip = nxt_ip
                lin = self._lin(cs, ip)

    def name(self, cs, ip):
        if (cs, ip) in self.functions:
            return "start" if (cs, ip) == self.entry else f"sub_{cs:04X}_{ip:04X}"
        return f"loc_{cs:04X}_{ip:04X}"

    def listing(self) -> str:
        func_lins = {self._lin(*f): f for f in self.functions}
        out = [
            "; Восстановлено dosrip. Адреса указаны как СЕГМЕНТ:СМЕЩЕНИЕ относительно начала образа.",
            f"; Точка входа: {self.entry[0]:04X}:{self.entry[1]:04X}; функций найдено: {len(self.functions)}",
            "",
        ]
        prev_end = None
        for lin in sorted(self.insns):
            cs, insn = self.insns[lin]
            if prev_end is not None and lin != prev_end:
                out.append(f"\n; ... {lin - prev_end:+d} байт данных/неразобранного кода ...\n" if lin > prev_end else "")
            if lin in func_lins:
                fcs, fip = func_lins[lin]
                out.append("")
                out.append(f"{self.name(fcs, fip)} proc")
            elif lin in self.labels:
                out.append(f"loc_{cs:04X}_{insn.address:04X}:")
            text = f"{insn.mnemonic:<7} {insn.op_str}".rstrip()
            text = self._symbolize(cs, insn, text)
            refs = [r for r in range(lin, lin + insn.size) if r in self.relocs]
            comment = self.comments.get(lin, "")
            if refs:
                comment = (comment + "; " if comment else "") + "сегментная ссылка (релокация)"
            hexb = insn.bytes.hex(" ")
            line = f"  {cs:04X}:{insn.address:04X}  {hexb:<24} {text}"
            if comment:
                line = f"{line:<70} ; {comment}"
            out.append(line)
            prev_end = lin + insn.size
        return "\n".join(out) + "\n"

    def _symbolize(self, cs, insn, text):
        m = insn.mnemonic
        ops = _imm_ops(insn)
        if m in ("lcall", "ljmp") and len(ops) == 2:
            return f"{m:<7} {self.name(ops[0], ops[1])}"
        if (m in ("call", "jmp") or m.startswith("j") or m.startswith("loop")) and len(ops) == 1 \
                and insn.operands[0].type == CS_OP_IMM:
            return f"{m:<7} {self.name(cs, ops[0] & 0xFFFF)}"
        return text

    def call_graph(self) -> str:
        """Текстовый граф вызовов: функция -> кого вызывает."""
        func_lins = sorted(self._lin(*f) for f in self.functions)
        calls = {}
        current = None
        for lin in sorted(self.insns):
            cs, insn = self.insns[lin]
            if lin in func_lins:
                current = self.name(cs, insn.address)
                calls.setdefault(current, [])
            ops = _imm_ops(insn)
            if current and insn.mnemonic in ("call", "lcall") and ops:
                tgt = self.name(ops[0], ops[1]) if len(ops) == 2 else self.name(cs, ops[0] & 0xFFFF)
                if tgt not in calls[current]:
                    calls[current].append(tgt)
        return "\n".join(f"{f} -> {', '.join(c) if c else '(нет вызовов)'}" for f, c in calls.items()) + "\n"

    @property
    def coverage(self) -> float:
        covered = sum(i.size for _cs, i in self.insns.values())
        return covered / len(self.image) if self.image else 0.0
