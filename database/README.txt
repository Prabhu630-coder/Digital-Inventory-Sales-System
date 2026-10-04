DATABASE MANAGEMENT

The app uses SQLite and stores its live database at:
    database/inventory.db

Best practice:
1. Use Business Settings -> Database Backup to download a timestamped .db backup.
2. Keep backups in a separate folder/cloud drive.
3. Before replacing/updating the project, download a fresh backup.
4. To move your data to a new copy of the project, close the old app and place the backup as database/inventory.db in the new project.
5. Do not delete inventory.db while the app is running.

The application includes safe startup migrations for newer settings, product units and invoice fields.
