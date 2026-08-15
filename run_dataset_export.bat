@echo off
REM Ежедневный экспорт датасета из подтверждённых человеком детекций
REM (см. sar_dataset_export.py). Ставится в планировщик Windows, например:
REM
REM   schtasks /create /tn "SAR Dataset Export" ^
REM     /tr "C:\путь\к\проекту\run_dataset_export.bat" /sc daily /st 13:00
REM
REM %~dp0 -- папка самого этого .bat, поэтому путь к проекту нигде не зашит
REM и файл одинаково работает на любой машине.
cd /d "%~dp0"
echo ==== %date% %time% ==== >> sar_data\dataset_export.log
python sar_dataset_export.py --out sar_dataset >> sar_data\dataset_export.log 2>&1
echo. >> sar_data\dataset_export.log
