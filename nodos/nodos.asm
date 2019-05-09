; Simple x86 16-bit boot code that prints NODOS to the console with ordinary BIOS call 10h

; compile with 'nasm nodos.asm' or 'fasm nodos.asm' (yasm premits an odd NULL WORD)
; check with 'ndisasm -o 78h nodos'

;bits 16					; FASM does not like this NASM directive
;org 5Ah					; boot code origin in boot sector after initial JMP (FAT32, EB 58 90)
org 78h					; boot code origin in boot sector after initial JMP (EXFAT, EB 76 90)

	mov ax, 0x7C0	; set DS with memory segment of this code
	mov ds, ax		; (DS should already be loaded with right value, however)
	mov si, nodos 	; characters array to load
teletype:
	lodsb					; load byte at address DS:(E)SI into AL
	or al, al 			; test if ending NULL
	jz halt				; if NULL, jump to 'halt' label
	mov ah, 0Eh		; BIOS function call
	mov bx, 7			; page
	int 10h				; int 10h, AH=0E: teletype character in AL
	jmp teletype		; repeat 'teletype' (until AL=0)
halt:						; loop HALTing CPU
	hlt
	jmp halt

nodos: db 'NO DOS' , 0
