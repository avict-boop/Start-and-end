; Игрушечный упаковщик: XOR 5Ah + применение релокаций + переход на OEP.
bits 16
org 0
  jmp short code
len_   dw 0
nrel   dw 0
oep_ip dw 0
oep_cs dw 0
oep_ss dw 0
oep_sp dw 0
code:
  mov ax, es
  add ax, 0x10
  mov bp, ax
  mov ds, ax
  xor si, si
  mov cx, [cs:len_]
.dec:
  xor byte [si], 0x5A
  inc si
  loop .dec
  mov cx, [cs:nrel]
  mov bx, reltab
.rel:
  jcxz .done
  mov si, [cs:bx]
  mov di, [cs:bx+2]
  add di, bp
  mov ds, di
  add [si], bp
  add bx, 4
  dec cx
  jmp .rel
.done:
  add [cs:oep_cs], bp
  mov ax, [cs:oep_ss]
  add ax, bp
  cli
  mov ss, ax
  mov sp, [cs:oep_sp]
  sti
  mov ax, es
  mov ds, ax
  jmp far [cs:oep_ip]
reltab:
