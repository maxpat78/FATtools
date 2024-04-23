@echo off
echo Updating and building optimized Cython exFAT module...
copy /Y %USERPROFILE%\AppData\Local\Programs\Python\Python311\Lib\site-packages\FATtools\exFAT.py exFAT.pyx
py setup_exFAT.py build_ext --inplace
REM ~ copy /Y exFAT.cp311*.pyd %USERPROFILE%\AppData\Local\Programs\Python\Python311\Lib\site-packages\FATtools
