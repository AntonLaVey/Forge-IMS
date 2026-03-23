# FORGE IMS git setup and deployment

## 1. Prepare your local project folder
1. Extract the project archive on your computer.
2. Open a terminal in the project root.
3. Initialize git:
   ```bash
   git init
   git branch -M main
   git add .
   git commit -m "Initial hardened FORGE IMS import"
   ```

## 2. Create a remote repository
Create a private GitHub, GitLab, or self-hosted repo named `forge-ims`.
Then connect your local copy:
```bash
git remote add origin <YOUR-REMOTE-URL>
git push -u origin main
```

## 3. Prepare the server one time
These steps assume Ubuntu or Debian.

### Install packages
```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip nginx postgresql-client
```

### Create the service user
```bash
sudo useradd --system --create-home --shell /bin/bash forge || true
```

### Create app folders
```bash
sudo mkdir -p /opt/forge-ims /var/www/forge-ims
sudo chown -R forge:forge /opt/forge-ims /var/www/forge-ims
```

### Clone the repo onto the server
```bash
sudo -u forge git clone <YOUR-REMOTE-URL> /opt/forge-ims
```

### Create backend environment file
```bash
sudo -u forge cp /opt/forge-ims/.env.example /opt/forge-ims/backend/.env
sudo -u forge nano /opt/forge-ims/backend/.env
```
Set at least:
- `APP_ENV=production`
- `DATABASE_URL=...`
- `SECRET_KEY=...`
- `ALLOWED_ORIGINS=...`
- `ALLOWED_HOSTS=...`

### Allow the deploy user to refresh the service
Create a sudoers file so the `forge` user can run the few commands the deploy script needs:
```bash
echo 'forge ALL=NOPASSWD: /bin/cp, /usr/bin/systemctl' | sudo tee /etc/sudoers.d/forge-ims-deploy
sudo chmod 440 /etc/sudoers.d/forge-ims-deploy
```

### First deploy
```bash
cd /opt/forge-ims
sudo -u forge ./scripts/deploy.sh
```

## 4. Point nginx at the app
Create `/etc/nginx/sites-available/forge-ims`:
```nginx
server {
    listen 80;
    server_name your-domain.example;

    root /var/www/forge-ims;
    index forge-ims-dashboard.html index.html;

    location / {
        try_files $uri $uri/ /forge-ims-dashboard.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
Then enable it:
```bash
sudo ln -sf /etc/nginx/sites-available/forge-ims /etc/nginx/sites-enabled/forge-ims
sudo nginx -t
sudo systemctl reload nginx
```

## 5. Deploy updates after you change code
On your computer:
```bash
git add .
git commit -m "Describe the update"
git push
```
On the server:
```bash
cd /opt/forge-ims
sudo -u forge git pull
sudo -u forge ./scripts/deploy.sh
```

## 6. Optional push-to-deploy later
If you want `git push` to trigger deployment automatically:
1. Create a bare repo on the server.
2. Use `scripts/post-receive.sample` as the `post-receive` hook.
3. Push to that bare repo instead of logging in and pulling.

## 7. Useful checks
```bash
sudo systemctl status forge-ims
sudo journalctl -u forge-ims -n 100 --no-pager
curl http://127.0.0.1:8000/health
```
