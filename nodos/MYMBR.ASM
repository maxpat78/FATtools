; A generic Master Boot Record for IBM PC compatible
; x86 16-bit boot code
;
; Works both with LBA and CHS partitions marked as bootable.
;
; compile with nasm mymbr.asm

; BIOS loads it at 7C00h, we relocate it to traditional address
org 600h

    ; set DS=SS=ES=0 and SP=code origin
    xor cx, cx
    cli
    mov ss, cx
    mov sp, 7C00h
    mov ds, cx
    mov es, cx
    sti

    mov bx, sp
    ; relocate 512 bytes (100h words) to 600h
    mov si, bx
    mov di, 600h
    mov cx, 100h
    cld
    repe movsw
    ; shorter?
    ;push cx
    ;push 0x61F
    ;retf
    jmp 0x0000:0x61F ; jump to (relocated) next instruction

    mov si, 7BEh ; partition table start at +1BEh (byte 80h/00h for bootable/not bootable)
IsBootablePart:
    cmp byte [si], 80h
    jz LoadInt13_42H
    add si, 10h ; part. entry size
    cmp si, 7FEh
    jl IsBootablePart

PrintMsg:
    mov si, NoBoot
PrintLoop:
    lodsb
    cmp al, 0
    jz Exit
    xor bx, bx
    mov ah, 0Eh ; output char in AL with attribute in BX
    int 10h
    jmp PrintLoop
Exit:
    ;int 18h ; diskless boot hook
    hlt
    jmp Exit

LoadInt13_42H:
    mov ah, 42h ; extended read in LBA mode from drive DL (preset by BIOS)
    xchg di, si
    mov si, dap ; disk address packet
    mov cx, [di+8] ; put LBA sector to read (LO-HI bytes) into dap
    mov [si + DAP.LBAlo], cx
    mov cx, [di+10]
    mov [si + DAP.LBAhi], cx
    ; stc
    int 13h
    xchg di, si ; partition pointer back to SI
    jnc CheckMagic ; else, try with CHS read

struc DAP
  .size    : resb 1
  .unused  : resb 1
  .sectors : resw 1
  .buffer   : resd 1
  .LBAlo   : resd 1
  .LBAhi   : resd 1
endstruc

dap:
    istruc DAP
    at DAP.size,    db 16
    at DAP.unused,  db 0
    at DAP.sectors, dw 1                 ; how many
    at DAP.buffer,   dd 00007C00h         ; buffer address (0000:7C00h)
    at DAP.LBAlo,   dd 0                 ; which sector?
    at DAP.LBAhi,   dd 0
    iend

LoadInt13_02H:
    mov ax, 0201h ; read 1 sector from drive DL, C-H-S in CH-DH-CL, to ES:BX
    ;mov bx, 7C00h ; pre-loaded above
    mov cx, [si+2] ; C-H-S in partition entry
    mov dh, [si+1]
    ; stc
    int 13h
    jc PrintMsg
CheckMagic:
    cmp word [7FEh], 0xAA55 ; magic in last sector word?
    jnz PrintMsg
    jmp 0x0000:0x7C00 ; jump to Boot Sector code, with DL=boot drive and DS:SI=partition booted

NoBoot:
db "Nothing to boot.", 0
