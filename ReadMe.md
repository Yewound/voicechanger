Voice Changer (Linux)

GUI-утилита изменения голоса в реальном времени (PyQt6 + numpy + PortAudio).Работает поверх PipeWire / PulseAudio / JACK / ALSA.
Запуск (portable)

./VoiceChanger-x86_64.AppImage# если в системе нет FUSE:./VoiceChanger-x86_64.AppImage --appimage-extract-and-run

Запуск из исходников

pip install PyQt6 numpy sounddevice   # + sudo apt install libportaudio2python3 app.py

Сборка AppImage

./build.sh        # → VoiceChanger-x86_64.AppImage

Скрипт сам ставит зависимости (в .venv), генерирует icon.png (если нет),собирает app.py через PyInstaller и упаковывает через appimagetool.
Использование

    Выберите вход (микрофон) и выход (наушники!) → «Старт».
    Говорите — слышите изменённый голос. Пресеты слева, параметры справа.

