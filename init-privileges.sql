-- User is created by MySQL from secrets (mysql_user, mysql_password).
-- This runs only on first container startup (fresh mysql_data volume).

-- Full access (including DROP, GRANT) on prod_*, plumoai_*, and authdb_prod
GRANT ALL PRIVILEGES ON `prod\_%`.* TO 'plumoai_user'@'%' WITH GRANT OPTION;
GRANT ALL PRIVILEGES ON `plumoai\_%`.* TO 'plumoai_user'@'%' WITH GRANT OPTION;
GRANT ALL PRIVILEGES ON `authdb_prod`.* TO 'plumoai_user'@'%' WITH GRANT OPTION;

-- Allow runtime database creation (API / user-click DB creation)
GRANT CREATE ON *.* TO 'plumoai_user'@'%';

FLUSH PRIVILEGES;
