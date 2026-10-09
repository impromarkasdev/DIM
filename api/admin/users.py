from __future__ import annotations
import logging
from urllib.parse import parse_qs, urlparse

from app.http_api import ApiHandler, AuthenticationError
from app.users import delete_user, list_users, upsert_user

logger = logging.getLogger(__name__)

class handler(ApiHandler):
    @staticmethod
    def _parse_active(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if value in ("true", "1", 1):
            return True
        if value in ("false", "0", 0):
            return False
        raise ValueError("active debe ser booleano")

    def do_GET(self) -> None:
        try:
            self.claims(admin=True)
            self.respond(200, {"success": True, "users": list_users()})
        except ValueError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except PermissionError:
            self.respond(403, {"success": False, "message": "Se requiere acceso de administrador."})
        except RuntimeError as exc:
            if "Database temporarily unavailable" in str(exc):
                self.respond(503, {"success": False, "message": "La base de datos está temporalmente ocupada. Inténtalo de nuevo."})
            else:
                logger.exception("User listing initialization failed")
                self.respond(500, {"success": False, "message": "No fue posible cargar los usuarios."})
        except Exception:
            logger.exception("Unexpected user listing error")
            self.respond(500, {"success": False, "message": "No fue posible cargar los usuarios."})
    def do_POST(self) -> None:
        try:
            self.claims(admin=True)
        except ValueError:
            self.respond(401, {"error":"Invalid or expired session"}); return
        except PermissionError:
            self.respond(403, {"error":"Forbidden"}); return
        except RuntimeError:
            logger.exception("Could not validate administrator session for user creation")
            self.respond(503, {"success": False, "message": "La autenticación está temporalmente ocupada. Inténtalo de nuevo."})
            return
        try:
            body=self.json_body()
            email = body.get("email")
            password = body.get("password")
            role = body.get("role", "client")
            if not isinstance(email, str) or (password is not None and not isinstance(password, str)) or not isinstance(role, str):
                raise ValueError("Los tipos de datos del usuario no son válidos")
            user = upsert_user(email, password, role, self._parse_active(body.get("active", True)))
            self.respond(200, {"status":"saved", "user": user})
        except (KeyError, ValueError):
            self.respond(400, {"success": False, "message": "Los datos del usuario no son válidos."})
        except RuntimeError as exc:
            if "Database temporarily unavailable" in str(exc):
                self.respond(503, {"success": False, "message": "La base de datos está temporalmente ocupada. Inténtalo de nuevo."})
            else:
                logger.exception("User save initialization failed")
                self.respond(500, {"success": False, "message": "No fue posible guardar el usuario."})
        except Exception:
            logger.exception("Unexpected user save error")
            self.respond(500, {"success": False, "message": "No fue posible guardar el usuario."})
    def do_DELETE(self) -> None:
        try:
            self.claims(admin=True)
            user_id = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            if not user_id: raise ValueError("id query parameter is required")
            if not delete_user(user_id): self.respond(404, {"error": "User not found or is an administrator"}); return
            self.respond(200, {"status": "deleted", "id": user_id})
        except AuthenticationError:
            self.respond(401, {"success": False, "message": "La sesión no es válida o expiró."})
        except PermissionError:
            self.respond(403, {"success": False, "message": "Se requiere acceso de administrador."})
        except ValueError as exc:
            self.respond(400, {"success": False, "message": str(exc)})
        except RuntimeError as exc:
            if "Database temporarily unavailable" in str(exc):
                self.respond(503, {"success": False, "message": "La base de datos está temporalmente ocupada. Inténtalo de nuevo."})
            else:
                logger.exception("User delete initialization failed")
                self.respond(500, {"success": False, "message": "No fue posible eliminar el usuario."})
        except Exception:
            logger.exception("Unexpected user delete error")
            self.respond(500, {"success": False, "message": "No fue posible eliminar el usuario."})
