# Contributing to ReelsBot

First off, thank you for considering contributing to ReelsBot! It is open-source projects like this that empower developers and creators worldwide.

---

## 🛠️ Development Setup

1. **Fork and Clone the Repository**:
   ```bash
   git clone https://github.com/your-username/reels-bot.git
   cd reels-bot
   ```

2. **Set up Virtual Environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. **Configure Local Environment**:
   ```bash
   cp .env.example .env
   # Add your Telegram Bot Token & API keys
   ```

---

## 🧪 Testing Guidelines

Before submitting any Pull Request, ensure that all automated tests pass:

```bash
pytest -v test_reels_bot.py
```

### Writing New Tests
- Keep all unit tests isolated with mocks (using `unittest.mock.patch` or `monkeypatch`).
- Follow the existing 4-tier test architecture:
  - **Tier 1**: Core logic, parsing, and duration guards.
  - **Tier 2**: External API adapters and fallbacks (Groq, Gemini, Cloudinary, Buffer).
  - **Tier 3**: Telegram Bot application and inline button handlers.
  - **Tier 4**: Non-destructive integration dry-runs.

---

## 📌 Code Style & Standards

- **Formatting**: PEP 8 standards with type hints where applicable.
- **Async/Await**: Ensure network I/O utilizes non-blocking `httpx.AsyncClient` or `asyncio`.
- **Security**: Never hardcode API keys, secrets, tokens, or personal chat IDs in code or commits. Always reference `.env`.

---

## 🚀 Submitting a Pull Request

1. Create a descriptive branch: `git checkout -b feature/your-feature-name`
2. Commit your changes: `git commit -m "feat: add support for XYZ"`
3. Push to your fork: `git push origin feature/your-feature-name`
4. Open a Pull Request on GitHub against `main`.

Thank you for helping make ReelsBot the best automation bot on GitHub!

