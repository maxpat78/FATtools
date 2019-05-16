@echo off
REM Stressa sequenzialmente il filesystem, con una sequenza pseudo-casuale a seme controllato
REM ~ py -3 mkfat.py g: -t exfat -c 512 && py -3 stress.py g: -t 15 --sha1 --fix --debug 8
set n=0
echo. %n% >seed.txt
del testit.log >NUL
for /L %%i in (%n% 1 50) do echo %%i >>testit.log && py -3 mkfat.py g: -t exfat -c 512 && py -3 stress.py g: -t 75 --sha1 --fixdriven && chkdsk g:  >>testit.log  && g: && rhash -c --skip-ok hashes.sha1 >>testit.log && c:
