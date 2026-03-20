"""Invoice management."""

import secrets
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Invoice, Subscription
from .config import settings

# Stripe import with fallback
try:
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    STRIPE_AVAILABLE = bool(settings.STRIPE_SECRET_KEY)
except ImportError:
    STRIPE_AVAILABLE = False


class InvoiceManager:
    """Manages invoices."""

    def _generate_invoice_number(self) -> str:
        """Generate unique invoice number."""
        timestamp = datetime.utcnow().strftime("%Y%m%d")
        random_part = secrets.token_hex(4).upper()
        return f"INV-{timestamp}-{random_part}"

    async def create_invoice(
        self,
        user_id: str,
        line_items: List[Dict[str, Any]],
        billing_info: Optional[Dict[str, Any]] = None,
        due_days: int = 30,
        db_session: AsyncSession = None,
    ) -> Invoice:
        """Create a new invoice."""
        # Calculate totals
        subtotal = Decimal("0")
        for item in line_items:
            item_total = Decimal(str(item.get("quantity", 1))) * Decimal(str(item.get("unit_price", 0)))
            item["total"] = float(item_total)
            subtotal += item_total

        tax = Decimal("0")  # Tax calculation would go here
        total = subtotal + tax

        invoice = Invoice(
            user_id=user_id,
            invoice_number=self._generate_invoice_number(),
            status="draft",
            subtotal=subtotal,
            tax=tax,
            total=total,
            amount_due=total,
            line_items=line_items,
            due_date=datetime.utcnow() + timedelta(days=due_days) if due_days else None,
            billing_name=billing_info.get("name") if billing_info else None,
            billing_email=billing_info.get("email") if billing_info else None,
            billing_address=billing_info.get("address") if billing_info else None,
        )
        db_session.add(invoice)
        await db_session.commit()
        await db_session.refresh(invoice)
        return invoice

    async def get_invoice(
        self,
        invoice_id: str,
        db_session: AsyncSession,
    ) -> Optional[Invoice]:
        """Get invoice by ID."""
        result = await db_session.execute(
            select(Invoice).where(Invoice.id == invoice_id)
        )
        return result.scalar_one_or_none()

    async def get_invoice_by_number(
        self,
        invoice_number: str,
        db_session: AsyncSession,
    ) -> Optional[Invoice]:
        """Get invoice by number."""
        result = await db_session.execute(
            select(Invoice).where(Invoice.invoice_number == invoice_number)
        )
        return result.scalar_one_or_none()

    async def list_invoices(
        self,
        user_id: str,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        db_session: AsyncSession = None,
    ) -> List[Invoice]:
        """List user's invoices."""
        stmt = select(Invoice).where(Invoice.user_id == user_id)

        if status:
            stmt = stmt.where(Invoice.status == status)

        stmt = stmt.order_by(Invoice.created_at.desc())
        stmt = stmt.limit(limit).offset(offset)

        result = await db_session.execute(stmt)
        return result.scalars().all()

    async def finalize_invoice(
        self,
        invoice_id: str,
        db_session: AsyncSession,
    ) -> Invoice:
        """Finalize a draft invoice."""
        invoice = await self.get_invoice(invoice_id, db_session)
        if not invoice:
            raise ValueError("Invoice not found")

        if invoice.status != "draft":
            raise ValueError("Only draft invoices can be finalized")

        # Create Stripe invoice if configured
        if STRIPE_AVAILABLE:
            # Get subscription for customer ID
            result = await db_session.execute(
                select(Subscription).where(Subscription.user_id == invoice.user_id)
            )
            subscription = result.scalar_one_or_none()

            if subscription and subscription.stripe_customer_id:
                stripe_invoice = stripe.Invoice.create(
                    customer=subscription.stripe_customer_id,
                    auto_advance=True,
                    metadata={
                        "invoice_id": str(invoice.id),
                        "invoice_number": invoice.invoice_number,
                    },
                )

                # Add line items
                for item in invoice.line_items or []:
                    stripe.InvoiceItem.create(
                        customer=subscription.stripe_customer_id,
                        invoice=stripe_invoice.id,
                        description=item.get("description", ""),
                        quantity=item.get("quantity", 1),
                        unit_amount=int(float(item.get("unit_price", 0)) * 100),
                        currency="usd",
                    )

                # Finalize
                stripe_invoice = stripe.Invoice.finalize_invoice(stripe_invoice.id)

                invoice.stripe_invoice_id = stripe_invoice.id
                invoice.stripe_invoice_pdf = stripe_invoice.invoice_pdf
                invoice.stripe_hosted_invoice_url = stripe_invoice.hosted_invoice_url

        invoice.status = "open"
        await db_session.commit()
        await db_session.refresh(invoice)
        return invoice

    async def mark_paid(
        self,
        invoice_id: str,
        amount_paid: Optional[Decimal] = None,
        db_session: AsyncSession = None,
    ) -> Invoice:
        """Mark invoice as paid."""
        invoice = await self.get_invoice(invoice_id, db_session)
        if not invoice:
            raise ValueError("Invoice not found")

        invoice.status = "paid"
        invoice.amount_paid = amount_paid or invoice.total
        invoice.amount_due = invoice.total - invoice.amount_paid
        invoice.paid_at = datetime.utcnow()

        await db_session.commit()
        await db_session.refresh(invoice)
        return invoice

    async def void_invoice(
        self,
        invoice_id: str,
        db_session: AsyncSession,
    ) -> Invoice:
        """Void an invoice."""
        invoice = await self.get_invoice(invoice_id, db_session)
        if not invoice:
            raise ValueError("Invoice not found")

        if invoice.status == "paid":
            raise ValueError("Cannot void a paid invoice")

        # Void in Stripe
        if STRIPE_AVAILABLE and invoice.stripe_invoice_id:
            stripe.Invoice.void_invoice(invoice.stripe_invoice_id)

        invoice.status = "void"
        await db_session.commit()
        await db_session.refresh(invoice)
        return invoice

    async def sync_from_stripe(
        self,
        stripe_invoice_id: str,
        db_session: AsyncSession,
    ) -> Invoice:
        """Sync invoice from Stripe."""
        if not STRIPE_AVAILABLE:
            raise ValueError("Stripe not configured")

        stripe_invoice = stripe.Invoice.retrieve(stripe_invoice_id)

        # Find or create invoice
        result = await db_session.execute(
            select(Invoice).where(Invoice.stripe_invoice_id == stripe_invoice_id)
        )
        invoice = result.scalar_one_or_none()

        if not invoice:
            # Get user from customer
            result = await db_session.execute(
                select(Subscription).where(
                    Subscription.stripe_customer_id == stripe_invoice.customer
                )
            )
            subscription = result.scalar_one_or_none()

            if not subscription:
                raise ValueError("No subscription found for customer")

            invoice = Invoice(
                user_id=subscription.user_id,
                stripe_invoice_id=stripe_invoice_id,
                invoice_number=self._generate_invoice_number(),
            )
            db_session.add(invoice)

        # Update from Stripe
        invoice.status = stripe_invoice.status
        invoice.subtotal = Decimal(str(stripe_invoice.subtotal / 100))
        invoice.tax = Decimal(str(stripe_invoice.tax or 0) / 100)
        invoice.total = Decimal(str(stripe_invoice.total / 100))
        invoice.amount_paid = Decimal(str(stripe_invoice.amount_paid / 100))
        invoice.amount_due = Decimal(str(stripe_invoice.amount_due / 100))
        invoice.currency = stripe_invoice.currency
        invoice.stripe_invoice_pdf = stripe_invoice.invoice_pdf
        invoice.stripe_hosted_invoice_url = stripe_invoice.hosted_invoice_url

        if stripe_invoice.period_start:
            invoice.period_start = datetime.fromtimestamp(stripe_invoice.period_start)
        if stripe_invoice.period_end:
            invoice.period_end = datetime.fromtimestamp(stripe_invoice.period_end)
        if stripe_invoice.due_date:
            invoice.due_date = datetime.fromtimestamp(stripe_invoice.due_date)

        # Extract line items
        line_items = []
        for item in stripe_invoice.lines.data:
            line_items.append({
                "description": item.description,
                "quantity": item.quantity,
                "unit_price": item.unit_amount / 100 if item.unit_amount else 0,
                "total": item.amount / 100,
            })
        invoice.line_items = line_items

        await db_session.commit()
        await db_session.refresh(invoice)
        return invoice

    async def get_invoice_stats(
        self,
        user_id: str,
        db_session: AsyncSession,
    ) -> Dict[str, Any]:
        """Get invoice statistics for user."""
        result = await db_session.execute(
            select(
                func.count(Invoice.id).label("total_invoices"),
                func.sum(Invoice.total).label("total_amount"),
                func.sum(Invoice.amount_paid).label("total_paid"),
            )
            .where(Invoice.user_id == user_id)
        )
        row = result.one()

        # Count by status
        status_result = await db_session.execute(
            select(Invoice.status, func.count(Invoice.id))
            .where(Invoice.user_id == user_id)
            .group_by(Invoice.status)
        )
        status_counts = {row[0]: row[1] for row in status_result.all()}

        return {
            "total_invoices": row.total_invoices or 0,
            "total_amount": float(row.total_amount or 0),
            "total_paid": float(row.total_paid or 0),
            "outstanding": float((row.total_amount or 0) - (row.total_paid or 0)),
            "by_status": status_counts,
        }


# Import timedelta for due_date calculation
from datetime import timedelta

invoice_manager = InvoiceManager()
