"""
Connect to Snowflake as admin using password + MFA passcode (no SnowSQL needed).
Run: python sf_mfa_setup.py
"""
import getpass, sys, os

# Load project .env for Snowflake vars
env_path = os.path.join(os.path.dirname(__file__), ".env")
for line in open(env_path).read().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

try:
    import snowflake.connector
except ImportError:
    print("Installing snowflake-connector-python...")
    os.system(f"{sys.executable} -m pip install snowflake-connector-python cryptography -q")
    import snowflake.connector

from cryptography.hazmat.primitives.serialization import load_pem_private_key, Encoding, PrivateFormat, NoEncryption, PublicFormat

# Read public key from private key file
key_path = os.path.join(os.path.dirname(__file__), "snowflake_key.p8")
with open(key_path, "rb") as f:
    priv = load_pem_private_key(f.read(), password=None)

pub_lines = priv.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode().splitlines()
pub_stripped = "".join(l for l in pub_lines if not l.startswith("---"))

ACCOUNT = "UVWIABH-FC16241"
ADMIN   = "RITWIKSHARAN"

print("=" * 55)
print("  Snowflake Admin Setup — careintel_svc user")
print("=" * 55)
print()
print("Enter your admin credentials below.")
print("(Nothing is stored — used only for this one-time setup)")
print()

password = getpass.getpass(f"Password for {ADMIN}: ")

print()
print("Open your MFA authenticator app and enter the")
print("current 6-digit code for your Snowflake account:")
passcode = input("MFA code: ").strip()

print()
print("Connecting to Snowflake...")

try:
    conn = snowflake.connector.connect(
        account=ACCOUNT,
        user=ADMIN,
        password=password,
        passcode=passcode,
        warehouse="COMPUTE_WH",
    )
    print("✅ Connected!\n")
except Exception as e:
    print(f"\n❌ Connection failed: {e}")
    print()
    print("Tips:")
    print("  - Make sure the MFA code is current (codes change every 30s)")
    print("  - Double-check your password")
    sys.exit(1)

cur = conn.cursor()

sqls = [
    ("Drop old user",       "DROP USER IF EXISTS careintel_svc"),
    ("Create service user", f"CREATE USER careintel_svc TYPE=SERVICE DEFAULT_ROLE=SYSADMIN DEFAULT_WAREHOUSE=COMPUTE_WH RSA_PUBLIC_KEY='{pub_stripped}'"),
    ("Grant SYSADMIN",      "GRANT ROLE SYSADMIN TO USER careintel_svc"),
    ("Verify",              "DESC USER careintel_svc"),
]

for label, sql in sqls:
    print(f"  Running: {label}...")
    try:
        cur.execute(sql)
        if label == "Verify":
            rows = cur.fetchall()
            for r in rows:
                if r[0] in ("RSA_PUBLIC_KEY", "TYPE", "DEFAULT_ROLE"):
                    print(f"    {r[0]} = {r[1]}")
        print(f"  ✅ Done")
    except Exception as e:
        print(f"  ❌ Failed: {e}")

cur.close()
conn.close()

print()
print("=" * 55)
print("  Setup complete!")
print("  Now run: python snowflake_setup.py")
print("=" * 55)
