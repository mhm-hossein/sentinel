# Sentinel Platform

ریپوی گلو‌وار و ایرانی خودتون رو بسازیم! 🇮🇷

---

[![CI](https://github.com/mhm-hossein/sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/mhm-hossein/sentinel/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## چیه این؟

**Sentinel** پلتفرم خودکارسازی (automation) برای خودبوت‌های تلگرام، طراحی
شده برای گروه‌های پرسنل:

- کاربران از طریق یک **بات‌کنترل** ورود می‌کنن، پلن می‌گیرن و چند تا نمونه‌ی
  تمیز از خودبوت خودشون رو مدیریت می‌کنن.
- نصب با **یه خط دستور** انجام می‌شه (سرویس `systemd` اوتوماتیک).
- کیفیت: CI با Python 3.11/3.12، 7 فاز‌ی تست، scan سیکرت.

---

## 📦 نصب (یک خط)

روی VPS تون به عنوان `root`:

```bash
curl -fsSL https://raw.githubusercontent.com/mhm-hossein/sentinel/main/install.sh | sudo bash
```

۴ تا از اون‌ها پرسیده می‌شه (یا اگه `SENTINEL_NONINTERACTIVE=1` باشه، خطا می‌ده):

| مقدار | کجایش می‌گیرید |
| :--- | --- |
| `SENTINEL_API_ID` / `SENTINEL_API_HASH` | [my.telegram.org](https://my.telegram.org) → API development tools |
| `CONTROL_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `SENTINEL_OWNER_ID` | [@userinfobot](https://t.me/userinfobot) |

**حالت بی‌توقف** (CI/Ansible): متغیرها رو `export` بکنین و خودداری می‌کنه:

```bash
export SENTINEL_API_ID=123456 SENTINEL_API_HASH=abcdef... \
       CONTROL_BOT_TOKEN=12345:AA... SENTINEL_OWNER_ID=123456789 \
       SENTINEL_NONINTERACTIVE=1
curl -fsSL https://raw.githubusercontent.com/mhm-hossein/sentinel/main/install.sh | sudo -E bash
```

گزینه‌های پیکربندی: `SENTINEL_INSTALL_DIR` (پیش‌فرض `/opt/sentinel`),
`SENTINEL_BRANCH`, `SENTINEL_SERVICE_USER`, `SENTINEL_SERVICE_NAME`,
`SENTINEL_PYTHON`.

پس از نصب: **/start** بزنین تا ویزارد ثبت‌نام رو کامل کنی. آیدی خودت (owner)
به‌صورت خودکار به پنل ادمین می‌رسه.

---

## 🚀 چرا نصب می‌کنیم؟

| ✅ | توضیح |
| --- | --- |
| ✅ | یه خط دستور؛ فایل‌های اولیه‌ی لازم نیست |
| ✅ | سرویس `systemd` می‌سازه و چک می‌کنه اگه واقعاً بالا اومده |
| ✅ | `.env` رو حفظ می‌کنه؛ دفعه دوم فقط کد رو آپدیت می‌کنه |
| ✅ | CI گرین: 3 پایتون، 7 فاز تست، scan سیکرت |
| ✅ | فیکس‌های مهم: `sudo` جداکننده، `git dubious ownership` |

> ⚠️ **ذاتبه‌سازی پرسنل‌های تلگرام (selfbot) مخالف شرایط‌نامه تلگرامیه** و
> می‌محدوردن، محدودکردن یا حظر دائمی آن‌ها رو داده. فقط اکانت‌های اختصاصی بگیر،
> فاصله مناسبی نگه دار، و مسئولیت کامنت‌هات رو بذاری.

---

## ℹ️ تخت لینکی

| فایل | موقعیت |
| --- | --- |
| `README.md` | 📖 English |
| `README.fa.md` | 🇮🇷 Persian |

سایت روی گیت‌هاب: [mhm-hossein/sentinel](https://github.com/mhm-hossein/sentinel)

---

## 📄 لایسنس

MIT — بخش `LICENSE`.

---

## ⚠️ هشدار

ذاتبه‌سازی پرسنل‌های تلگرام (selfbot) مخالف شرایط‌نامه تلگرامیه و می‌محدودیت،
محدودکردن یا حظر دائمی آن‌ها رو داده. فقط اکانت‌های اختصاصی بگیر، فاصله مناسبی
نگه دار، و مسئولیت کامنت‌هات رو بذاری.
