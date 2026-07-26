nano requirements.txt                  # paste: python-telegram-bot>=20.0
nano render.yaml                       # paste the render config

# 3. Push to GitHub
git init
git add .
git commit -m "hosting bot"
git branch -M main
git remote add origin https://github.com/Beasgohan-code/hosting-bot.git
git push -u origin main

