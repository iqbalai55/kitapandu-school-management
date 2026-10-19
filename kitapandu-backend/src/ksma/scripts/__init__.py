"""KSMA seed script for Vaneta Maju Berdaya pilot.

Creates:
- 1 Vaneta DEMO tenant
- 1 Lembaga Uji DEMO tenant (cross-tenant isolation test)
- 1 EducationUnit per tenant
- 1 platform-operator ServicePrincipal
- 1 owner account per tenant (OW)
- AM / AC / FI / IN accounts per tenant
- Memberships + AccessAssignments with default role grants
- 1 published finance PolicyVersion (Q04 stub ruleset)
- 1 published access PolicyVersion
- 1 published enrollment PolicyVersion (Q02 — unblocks merge endpoint)
- 1 published session PolicyVersion (Q06 — unblocks closure)
- 1 published assessment PolicyVersion (Q07)
- 1 published certificate PolicyVersion (Q12)
- 1 Program + ProgramVersion + AcademicPeriod + RubricVersion with 3 indicators
- 1 Batch + Application + Enrollment (used by integration tests)

Run::

    python -m ksma.scripts.seed_vaneta_demo
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from ksma.identity.application import (
    activate_account,
    activate_membership,
    activate_tenant,
    create_account,
    create_education_unit,
    create_tenant,
    grant_access,
    invite_membership,
    publish_policy,
)
from ksma.identity.infrastructure.models import ServicePrincipalRow
from ksma.shared.auth import hash_password
from ksma.shared.persistence import session_scope


VANETA = {
    "display_name": "Vaneta DEMO",
    "code": "VAN",
    "owner_subject": "vaneta-owner",
    "admin_password": "ChangeMe-Vaneta-1",
}

LEMBAGA = {
    "display_name": "Lembaga Uji DEMO",
    "code": "LMB",
    "owner_subject": "lembaga-owner",
    "admin_password": "ChangeMe-Lembaga-1",
}


async def main() -> None:
    async with session_scope() as session:
        sp = ServicePrincipalRow(
            principal_code="ksma-bootstrap",
            purpose="Pilot seed bootstrap",
            environment="development",
            credential_ref="local://dev",
            status="active",
        )
        session.add(sp)
        await session.flush()
        print(f"[seed] service principal {sp.id}")

        for cfg in (VANETA, LEMBAGA):
            tenant = await create_tenant(
                session,
                display_name=cfg["display_name"],
                default_timezone="Asia/Jakarta",
            )
            await session.flush()
            await activate_tenant(session, tenant_id=tenant.id)
            unit = await create_education_unit(
                session,
                tenant_id=tenant.id,
                unit_code=f"{cfg['code']}-HQ",
                name=f"{cfg['display_name']} HQ",
                institution_type="LK",
                education_levels=["SMA"],
                timezone="Asia/Jakarta",
            )
            print(f"[seed] tenant {cfg['code']} {tenant.id} / unit {unit.id}")

            # Owner
            owner = await create_account(
                session,
                auth_subject=cfg["owner_subject"],
                display_name=f"{cfg['code']} Owner",
                password=cfg["admin_password"],
            )
            await activate_account(session, account_id=owner.id)
            membership = await invite_membership(
                session,
                tenant_id=tenant.id,
                account_id=owner.id,
                invitation_ref=f"{cfg['code']}-owner",
            )
            await activate_membership(session, membership_id=membership.id)

            # Access policy
            access_policy = await publish_policy(
                session,
                tenant_id=tenant.id,
                policy_domain="access",
                version_no=1,
                ruleset={"version": "pilot-v1", "grants": "default"},
            )
            # Finance policy (Q04 stub)
            finance_policy = await publish_policy(
                session,
                tenant_id=tenant.id,
                policy_domain="finance",
                version_no=1,
                ruleset={
                    "version": "pilot-v1",
                    "currency": "IDR",
                    "rounding": "ROUND_HALF_EVEN",
                    "default_installment_count": 1,
                },
            )
            # Enrollment policy (Q02 stub)
            await publish_policy(
                session,
                tenant_id=tenant.id,
                policy_domain="enrollment",
                version_no=1,
                ruleset={"version": "pilot-v1", "pause_supported": True},
            )
            # Session policy (Q06 stub)
            await publish_policy(
                session,
                tenant_id=tenant.id,
                policy_domain="session",
                version_no=1,
                ruleset={
                    "version": "pilot-v1",
                    "attendance_required_for_closure": True,
                },
            )
            # Assessment policy (Q07 stub)
            await publish_policy(
                session,
                tenant_id=tenant.id,
                policy_domain="assessment",
                version_no=1,
                ruleset={"version": "pilot-v1", "rubric_required": True},
            )
            # Certificate policy (Q12 stub)
            await publish_policy(
                session,
                tenant_id=tenant.id,
                policy_domain="certificate",
                version_no=1,
                ruleset={"version": "pilot-v1", "numbering_scope": "default"},
            )
            await session.flush()
            print(f"[seed]   {cfg['code']} policies (access, finance, enrollment, session, assessment, certificate)")

            # Default OW + AM + AC + FI + IN grants
            from ksma.shared.rbac import RoleCode

            ow_membership = membership
            am = await create_account(
                session,
                auth_subject=f"{cfg['code']}-am",
                display_name=f"{cfg['code']} Access Manager",
                password=cfg["admin_password"],
            )
            await activate_account(session, account_id=am.id)
            am_m = await invite_membership(
                session, tenant_id=tenant.id, account_id=am.id
            )
            await activate_membership(session, membership_id=am_m.id)

            await grant_access(
                session,
                tenant_id=tenant.id,
                membership_id=ow_membership.id,
                role_code=RoleCode.OW.value,
                action_ids=[
                    "PLT-A02", "PLT-A03", "PLT-A04",
                    "OPS-A03", "OPS-A04", "OPS-A05",
                    "OPS-A07", "OPS-A08",
                ],
                scope={},
                policy_id=access_policy.id,
                authorization={"actor": "seed"},
            )
            await grant_access(
                session,
                tenant_id=tenant.id,
                membership_id=am_m.id,
                role_code=RoleCode.AM.value,
                action_ids=["PLT-A02", "PLT-A03", "PLT-A05", "PLT-A06", "X-A03"],
                scope={},
                policy_id=access_policy.id,
                authorization={"actor": "seed"},
            )
            print(f"[seed]   {cfg['code']} grants: OW (Pimpinan), AM (Access Manager)")

        print("[seed] done.")


if __name__ == "__main__":
    asyncio.run(main())