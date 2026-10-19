"""KSMA Identity bounded context.

Owns Tenant, EducationUnit, Account, Membership, AccessAssignment,
ServicePrincipal, SupportGrant, PolicyVersion, AuditRecord. Every other
context depends on its invariants (tenant_id, unit_id, current membership).

Design notes
------------
- Domain layer: pure entities + value objects, no I/O.
- Application layer: use cases / services. Depends on domain + shared/.
- Infrastructure layer: SQLAlchemy models, repositories.
- Interface layer: FastAPI routers, Pydantic schemas.
"""