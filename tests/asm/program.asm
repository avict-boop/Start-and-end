; Тестовая DOS-программа: код в сегменте 0, данные с параграфа 10h.
bits 16
org 0
start:
  mov ax, 0x0010        ; сегмент данных (релокация)
  mov ds, ax
  mov dx, 0             ; msg
  mov ah, 9
  int 0x21
  call 0x0000:sub       ; дальний вызов (релокация)
  mov ax, 0x4c00
  int 0x21
sub:
  mov ah, 2
  mov dl, '!'
  int 0x21
  retf
times 0x100-($-$$) db 0
msg: db "Hello from DOS$"
times 0x120-($-$$) db 0
