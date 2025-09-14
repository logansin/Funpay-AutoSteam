# Funpay AutoSteam
🚀 Бот для автоматического пополнение Steam (СНГ) на FunPay  
📌 Сейчас в стадии бета-тестирования
      
## Что из себя представляет бот?  
Это Python-скрипт, который:  
✔ Коммиссия 0%.   
✔ Пополняет весь СНГ регион.   
✔ Проверка логина, можно ли его пополнить или нет .  
✔ Деактивирует лоты, если баланс меньше, чем вы настроили.  
  
## Что нужно для работы бота?  
1. Установка Python и библиотек
```pip install -r requirements.txt```
2. Зарегистироваться на [сайте](https://пополнистим.рф/)
3. Настройка .env
```
FUNPAY_AUTH_TOKEN=FUNPAY_AUTH_TOKEN
STEAM_API_USER=STEAM_API_USER
STEAM_API_PASS=STEAM_API_PASS
MIN_BALANCE = 1
AUTO_REFUND = true/false
AUTO_DEACTIVATE = true/false
```

Более подробная [Инструкция](https://teletype.in/@tinechelovec/Funpay-AutoSteam)
   
По всем багам, вопросам и предложениям пишите в [Issues](https://github.com/tinechelovec/Funpay-AutoSteam/issues) или в [Telegram](https://t.me/tinechelovec)

Другие боты и плагины [Channel](https://t.me/by_thc)
