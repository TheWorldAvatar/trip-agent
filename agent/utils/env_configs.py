import os


def retrieve_default_settings():
    global NAMESPACE, DATABASE, KEYCLOAK_SERVER, KEYCLOAK_REALM

    NAMESPACE = os.getenv("NAMESPACE")
    if NAMESPACE is None:
        NAMESPACE = 'kb'

    DATABASE = os.getenv('DATABASE')
    if DATABASE is None:
        DATABASE = 'postgres'

    KEYCLOAK_SERVER = os.getenv('KEYCLOAK_SERVER')
    KEYCLOAK_REALM = os.getenv('KEYCLOAK_REALM')


# run when module is imported
retrieve_default_settings()
