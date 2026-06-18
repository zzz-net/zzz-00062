from fastapi import Header, HTTPException, Depends
from sqlalchemy.orm import Session
from .database import get_db
from . import crud

ROLE_ADMIN = "admin"
ROLE_APPROVER = "approver"
ROLE_USER = "user"

ALLOW_RELEASE_ROLES = [ROLE_ADMIN, ROLE_APPROVER]
ALLOW_ROLLBACK_ROLES = [ROLE_ADMIN]
ALLOW_IMPORT_ROLES = [ROLE_ADMIN, ROLE_APPROVER, ROLE_USER]
ALLOW_CALCULATE_ROLES = [ROLE_ADMIN, ROLE_APPROVER, ROLE_USER]
ALLOW_CANDIDATE_ROLES = [ROLE_ADMIN, ROLE_APPROVER, ROLE_USER]
ALLOW_ARCHIVE_VIEW_ROLES = [ROLE_ADMIN, ROLE_APPROVER, ROLE_USER]
ALLOW_ARCHIVE_EXPORT_ROLES = [ROLE_ADMIN, ROLE_APPROVER, ROLE_USER]
ALLOW_ARCHIVE_AUDIT_ROLES = [ROLE_ADMIN]
ALLOW_ARCHIVE_CANCEL_ROLES = [ROLE_ADMIN, ROLE_APPROVER, ROLE_USER]
ALLOW_ARCHIVE_EXECUTE_ROLES = [ROLE_ADMIN, ROLE_APPROVER, ROLE_USER]


def get_current_user(x_username: str = Header(None), db: Session = Depends(get_db)):
    if not x_username:
        raise HTTPException(status_code=401, detail="未提供用户名，请在 X-Username 头中提供")
    user = crud.get_user(db, x_username)
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


def require_role(allowed_roles: list):
    def role_checker(user=Depends(get_current_user)):
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"权限不足，需要以下角色之一: {', '.join(allowed_roles)}，当前角色: {user.role}"
            )
        return user
    return role_checker
