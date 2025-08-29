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
FUNPAY_AUTH_TOKEN=ваш_золотой_ключ_FunPay  
STEAM_API_USER=ваш_ник_на_сайте 
STEAM_API_PASS-=ваш_пароль_на_сайте  
FRAGMENT_MNEMONICS=минимальный баланс при котором лоты будут деактивироваться
```

Более подробная [Инструкция](https://teletype.in/@tinechelovec/Funpay-AutoSteam)
   
По всем багам, вопросам и предложениям пишите в [Issues](https://github.com/tinechelovec/Funpay-AutoSteam/issues) или в [Telegram](https://t.me/tinechelovec)

Другие боты и плагины [Channel](https://t.me/by_thc)
