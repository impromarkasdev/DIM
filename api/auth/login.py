from __future__ import annotations
import logging
from app.http_api import ApiHandler
from app.security import create_jwt
from app.users import authenticate, clear_login_failures, login_is_rate_limited, record_login_failure
import os

logger = logging.getLogger(__name__)

class handler(ApiHandler):
    def do_POST(self) -> None:
        try:
            body = self.json_body()
            email = str(body.get("email", "")).strip().lower()
            password = str(body.get("password", ""))
            if not email or len(email) > 254 or not password or len(password) > 1024 or "@" not in email:
                raise ValueError("Email and password are required")
            if login_is_rate_limited(email):
                self.respond(429, {"success": False, "message": "Demasiados intentos. Espera 15 minutos antes de volver a intentarlo."})
                return
            user = authenticate(email, password)
            if not user:
                record_login_failure(email)
                self.respond(401, {"success": False, "message": "Correo o contraseña incorrectos."})
                return
            clear_login_failures(email)
            jwt_secret = os.getenv("JWT_SECRET", "")
            if len(jwt_secret.encode("utf-8")) < 32:
                logger.error("JWT_SECRET is missing or shorter than 32 bytes")
                self.respond(500, {"success": False, "message": "El servicio de autenticación no está configurado."})
                return
            token = create_jwt({"sub": user["email"], "role": user["role"]}, jwt_secret)
            self.respond(200, {"success": True, "token": token, "role": user["role"]})
        except ValueError:
            self.respond(400, {"success": False, "message": "Invalid login request"})
        except RuntimeError as exc:
            if "Database temporarily unavailable" in str(exc):
                self.respond(503, {"success": False, "message": "El servicio de autenticación está temporalmente ocupado. Inténtalo de nuevo."})
            else:
                logger.exception("Login configuration or initialization failed")
                self.respond(500, {"success": False, "message": "No fue posible inicializar la autenticación."})
        except Exception:
            logger.exception("Unexpected login error")
            self.respond(500, {"success": False, "message": "No fue posible completar el inicio de sesión."})
