\getenv app_password BOT_DB_PASSWORD
CREATE ROLE roleplay LOGIN PASSWORD :'app_password';
ALTER DATABASE roleplay OWNER TO roleplay;
GRANT USAGE, CREATE ON SCHEMA public TO roleplay;
