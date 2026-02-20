# Disciple.live MVP

A Flask-based discipleship connection platform inspired by matching apps. It supports:

- **Role-based logins** for Admin, Discipler (mentor), and Disciplee.
- **Mentorship requests** from disciplees to disciplers.
- **Acceptance workflow** for disciplers to approve or decline requests.
- **Availability scheduling** where disciplers post slots and disciplees book from accepted mentors.
- **Live video sessions** using Jitsi Meet (camera, mic, and screen-share).
- **Interactive content library** managed in the admin portal (file uploads + external links).

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000`.

Default admin account:

- Email: `admin@disciple.live`
- Password: `admin123`

## Suggested Production Next Steps

1. Add Stripe/subscription and payment logic.
2. Add email notifications and reminders.
3. Introduce granular permissions and audit logs.
4. Replace Jitsi iframe with a first-party WebRTC service if needed.
5. Add discipleship progress tracking, journals, prayer requests, and group cohorts.
