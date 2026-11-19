import sqlite3
import bcrypt

# Conectar a la base de datos actual
conn = sqlite3.connect("joma_inventory.db")
cursor = conn.cursor()

# Generar el nuevo hash seguro para la contraseña JOMA2026
nueva_password = "JOMA2026"
hashed = bcrypt.hashpw(nueva_password.encode('utf-8'), bcrypt.gensalt())

# Actualizar la clave para el usuario admin
cursor.execute(
    "UPDATE usuarios SET password_hash = ? WHERE username = 'admin'",
    (hashed,)
)
conn.commit()
conn.close()

print("¡Contraseña actualizada con éxito a JOMA2026!")