# FORGE IMS deployment notes

This project now includes:
- signed bearer tokens instead of server-stored active sessions
- login throttling and lockout protection
- hardened systemd service settings
- one-command deploy script
- environment-driven secrets and origin settings
- restored API route mounting for cycle count, budget, and NCM

See `SETUP_GIT_AND_DEPLOY.md` for a full server setup and update workflow.
