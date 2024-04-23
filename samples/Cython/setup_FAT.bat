@echo off
echo Updating and building optimized Cython FAT module...
copy /Y %USERPROFILE%\AppData\Local\Programs\Python\Python311\Lib\site-packages\FATtools\FAT.py FAT.pyx
py setup_FAT.py build_ext --inplace
REM ~ copy /Y exFAT.cp311*.pyd %USERPROFILE%\AppData\Local\Programs\Python\Python311\Lib\site-packages\FATtools
