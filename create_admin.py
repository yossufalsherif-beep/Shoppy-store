import getpass,sqlite3
from werkzeug.security import generate_password_hash
from app import init,db
init()
with db() as c:
 if c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():raise SystemExit('Owner already exists; no second owner can be created.')
 email=input('Owner email: ').strip().lower();pw=getpass.getpass('Owner password (min 12 chars): ')
 if len(pw)<12 or '@' not in email:raise SystemExit('Invalid email or weak password')
 c.execute('INSERT INTO users(email,password,role) VALUES(?,?,?)',(email,generate_password_hash(pw),'admin'))
print('Owner account created. Log in using /login')
