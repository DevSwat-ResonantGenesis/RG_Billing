"""
Invoice PDF Generation - Phase 3.1 GTM

Generate professional PDF invoices for billing records.
"""

import logging
import io
from datetime import datetime
from typing import Dict, Any, Optional, List
from decimal import Decimal

logger = logging.getLogger(__name__)

# Try to import PDF generation libraries
try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
    from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False
    logger.warning("reportlab not installed - PDF generation disabled")


# Company info (would come from config in production)
COMPANY_INFO = {
    "name": "ResonantGenesis",
    "address": "123 AI Street, Tech City, TC 12345",
    "email": "billing@resonantgenesis.com",
    "phone": "+1 (555) 123-4567",
    "website": "https://resonantgenesis.com",
    "tax_id": "XX-XXXXXXX",
}

# Credit to USD conversion
CREDIT_VALUE = 0.001  # 1 credit = $0.001


class InvoicePDFGenerator:
    """
    Generate professional PDF invoices.
    
    Features:
    - Company branding
    - Itemized line items
    - Usage breakdown
    - Payment terms
    - Multiple formats (letter, A4)
    """
    
    def __init__(self, page_size=letter):
        self.page_size = page_size
        self.styles = None
        if REPORTLAB_AVAILABLE:
            self._setup_styles()
    
    def _setup_styles(self):
        """Setup PDF styles."""
        self.styles = getSampleStyleSheet()
        
        # Custom styles
        self.styles.add(ParagraphStyle(
            name='InvoiceTitle',
            parent=self.styles['Heading1'],
            fontSize=24,
            spaceAfter=30,
            textColor=colors.HexColor('#1a1a2e'),
        ))
        
        self.styles.add(ParagraphStyle(
            name='SectionHeader',
            parent=self.styles['Heading2'],
            fontSize=14,
            spaceBefore=20,
            spaceAfter=10,
            textColor=colors.HexColor('#16213e'),
        ))
        
        self.styles.add(ParagraphStyle(
            name='CompanyName',
            parent=self.styles['Normal'],
            fontSize=18,
            textColor=colors.HexColor('#0f3460'),
            spaceAfter=5,
        ))
        
        self.styles.add(ParagraphStyle(
            name='RightAlign',
            parent=self.styles['Normal'],
            alignment=TA_RIGHT,
        ))
    
    def generate(
        self,
        invoice_data: Dict[str, Any],
        transactions: Optional[List[Dict]] = None,
    ) -> bytes:
        """
        Generate PDF invoice.
        
        Args:
            invoice_data: Invoice details including:
                - invoice_number
                - invoice_date
                - due_date
                - customer (name, email, address)
                - line_items
                - subtotal, tax, total
                - notes
            transactions: Optional list of credit transactions
            
        Returns:
            PDF as bytes
        """
        if not REPORTLAB_AVAILABLE:
            raise RuntimeError("reportlab not installed - cannot generate PDF")
        
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=self.page_size,
            rightMargin=0.75*inch,
            leftMargin=0.75*inch,
            topMargin=0.75*inch,
            bottomMargin=0.75*inch,
        )
        
        elements = []
        
        # Header
        elements.extend(self._build_header(invoice_data))
        
        # Invoice details
        elements.extend(self._build_invoice_details(invoice_data))
        
        # Line items
        elements.extend(self._build_line_items(invoice_data.get("line_items", [])))
        
        # Totals
        elements.extend(self._build_totals(invoice_data))
        
        # Transaction details (if provided)
        if transactions:
            elements.extend(self._build_transactions(transactions))
        
        # Notes and terms
        elements.extend(self._build_footer(invoice_data))
        
        # Build PDF
        doc.build(elements)
        
        pdf_bytes = buffer.getvalue()
        buffer.close()
        
        return pdf_bytes
    
    def _build_header(self, data: Dict) -> List:
        """Build invoice header with company info."""
        elements = []
        
        # Company name
        elements.append(Paragraph(COMPANY_INFO["name"], self.styles['CompanyName']))
        elements.append(Paragraph(COMPANY_INFO["address"], self.styles['Normal']))
        elements.append(Paragraph(f"Email: {COMPANY_INFO['email']}", self.styles['Normal']))
        elements.append(Paragraph(f"Phone: {COMPANY_INFO['phone']}", self.styles['Normal']))
        
        elements.append(Spacer(1, 30))
        
        # Invoice title
        elements.append(Paragraph("INVOICE", self.styles['InvoiceTitle']))
        
        return elements
    
    def _build_invoice_details(self, data: Dict) -> List:
        """Build invoice details section."""
        elements = []
        
        # Create two-column layout for invoice details and customer info
        invoice_info = [
            ["Invoice Number:", data.get("invoice_number", "N/A")],
            ["Invoice Date:", self._format_date(data.get("invoice_date"))],
            ["Due Date:", self._format_date(data.get("due_date"))],
            ["Period:", f"{self._format_date(data.get('period_start'))} - {self._format_date(data.get('period_end'))}"],
        ]
        
        customer = data.get("customer", {})
        customer_info = [
            ["Bill To:", ""],
            ["", customer.get("name", "N/A")],
            ["", customer.get("email", "")],
            ["", customer.get("address", "")],
        ]
        
        # Combine into table
        combined_data = []
        max_rows = max(len(invoice_info), len(customer_info))
        
        for i in range(max_rows):
            row = []
            if i < len(invoice_info):
                row.extend(invoice_info[i])
            else:
                row.extend(["", ""])
            row.append("")  # Spacer
            if i < len(customer_info):
                row.extend(customer_info[i])
            else:
                row.extend(["", ""])
            combined_data.append(row)
        
        table = Table(combined_data, colWidths=[1.2*inch, 1.8*inch, 0.5*inch, 0.8*inch, 2.5*inch])
        table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTNAME', (3, 0), (3, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        
        elements.append(table)
        elements.append(Spacer(1, 30))
        
        return elements
    
    def _build_line_items(self, items: List[Dict]) -> List:
        """Build line items table."""
        elements = []
        
        elements.append(Paragraph("Line Items", self.styles['SectionHeader']))
        
        # Table header
        table_data = [["Description", "Quantity", "Unit Price", "Amount"]]
        
        # Add items
        for item in items:
            table_data.append([
                item.get("description", ""),
                str(item.get("quantity", 1)),
                f"${float(item.get('unit_price', 0)):.4f}",
                f"${float(item.get('total', 0)):.2f}",
            ])
        
        if not items:
            table_data.append(["No items", "", "", ""])
        
        table = Table(table_data, colWidths=[3.5*inch, 1*inch, 1.2*inch, 1.2*inch])
        table.setStyle(TableStyle([
            # Header
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a1a2e')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
            
            # Body
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('ALIGN', (1, 1), (-1, -1), 'RIGHT'),
            ('ALIGN', (0, 1), (0, -1), 'LEFT'),
            
            # Grid
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f5f5f5')]),
            
            # Padding
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ]))
        
        elements.append(table)
        elements.append(Spacer(1, 20))
        
        return elements
    
    def _build_totals(self, data: Dict) -> List:
        """Build totals section."""
        elements = []
        
        subtotal = float(data.get("subtotal", 0))
        tax = float(data.get("tax", 0))
        total = float(data.get("total", 0))
        amount_paid = float(data.get("amount_paid", 0))
        amount_due = float(data.get("amount_due", total))
        
        totals_data = [
            ["", "Subtotal:", f"${subtotal:.2f}"],
            ["", "Tax:", f"${tax:.2f}"],
            ["", "Total:", f"${total:.2f}"],
        ]
        
        if amount_paid > 0:
            totals_data.append(["", "Amount Paid:", f"${amount_paid:.2f}"])
        
        totals_data.append(["", "Amount Due:", f"${amount_due:.2f}"])
        
        table = Table(totals_data, colWidths=[4.5*inch, 1.2*inch, 1.2*inch])
        table.setStyle(TableStyle([
            ('FONTNAME', (1, 0), (1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
            
            # Highlight total and amount due
            ('FONTNAME', (0, 2), (-1, 2), 'Helvetica-Bold'),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, -1), (-1, -1), 12),
            ('TEXTCOLOR', (2, -1), (2, -1), colors.HexColor('#0f3460')),
            
            # Line above total
            ('LINEABOVE', (1, 2), (-1, 2), 1, colors.black),
        ]))
        
        elements.append(table)
        elements.append(Spacer(1, 30))
        
        return elements
    
    def _build_transactions(self, transactions: List[Dict]) -> List:
        """Build transaction details section."""
        elements = []
        
        elements.append(Paragraph("Credit Usage Details", self.styles['SectionHeader']))
        
        # Limit to most recent 20 transactions
        recent = transactions[:20]
        
        table_data = [["Date", "Type", "Description", "Credits", "Balance"]]
        
        for tx in recent:
            table_data.append([
                self._format_date(tx.get("created_at")),
                tx.get("type", ""),
                tx.get("description", "")[:40],
                str(tx.get("amount", 0)),
                str(tx.get("balance_after", 0)),
            ])
        
        table = Table(table_data, colWidths=[1.2*inch, 0.8*inch, 2.5*inch, 1*inch, 1*inch])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#16213e')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('ALIGN', (3, 0), (-1, -1), 'RIGHT'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f8f8')]),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        
        elements.append(table)
        
        if len(transactions) > 20:
            elements.append(Spacer(1, 5))
            elements.append(Paragraph(
                f"Showing 20 of {len(transactions)} transactions",
                self.styles['Normal']
            ))
        
        elements.append(Spacer(1, 20))
        
        return elements
    
    def _build_footer(self, data: Dict) -> List:
        """Build footer with notes and terms."""
        elements = []
        
        # Notes
        notes = data.get("notes")
        if notes:
            elements.append(Paragraph("Notes", self.styles['SectionHeader']))
            elements.append(Paragraph(notes, self.styles['Normal']))
            elements.append(Spacer(1, 15))
        
        # Payment terms
        elements.append(Paragraph("Payment Terms", self.styles['SectionHeader']))
        elements.append(Paragraph(
            "Payment is due within 30 days of invoice date. "
            "Please include the invoice number with your payment.",
            self.styles['Normal']
        ))
        elements.append(Spacer(1, 10))
        
        # Credit info
        elements.append(Paragraph(
            f"Credit Value: 1 credit = ${CREDIT_VALUE:.4f} USD",
            self.styles['Normal']
        ))
        
        # Thank you
        elements.append(Spacer(1, 30))
        elements.append(Paragraph(
            "Thank you for your business!",
            ParagraphStyle(
                name='ThankYou',
                parent=self.styles['Normal'],
                fontSize=12,
                textColor=colors.HexColor('#0f3460'),
                alignment=TA_CENTER,
            )
        ))
        
        return elements
    
    def _format_date(self, date_value) -> str:
        """Format date for display."""
        if not date_value:
            return "N/A"
        
        if isinstance(date_value, str):
            try:
                date_value = datetime.fromisoformat(date_value.replace('Z', '+00:00'))
            except:
                return date_value
        
        if isinstance(date_value, datetime):
            return date_value.strftime("%B %d, %Y")
        
        return str(date_value)


# Global instance
invoice_pdf_generator = InvoicePDFGenerator()


async def generate_invoice_pdf(
    invoice_id: str,
    db_session,
) -> bytes:
    """
    Generate PDF for an invoice.
    
    Args:
        invoice_id: Invoice ID
        db_session: Database session
        
    Returns:
        PDF bytes
    """
    from .invoices import invoice_manager
    from .models import CreditTransaction
    from sqlalchemy import select
    
    # Get invoice
    invoice = await invoice_manager.get_invoice(invoice_id, db_session)
    if not invoice:
        raise ValueError("Invoice not found")
    
    # Get transactions for the period
    transactions = []
    if invoice.period_start and invoice.period_end:
        result = await db_session.execute(
            select(CreditTransaction)
            .where(
                CreditTransaction.user_id == invoice.user_id,
                CreditTransaction.created_at >= invoice.period_start,
                CreditTransaction.created_at <= invoice.period_end,
            )
            .order_by(CreditTransaction.created_at.desc())
        )
        tx_list = result.scalars().all()
        transactions = [
            {
                "created_at": tx.created_at,
                "type": tx.tx_type,
                "description": tx.description,
                "amount": tx.amount,
                "balance_after": tx.balance_after,
            }
            for tx in tx_list
        ]
    
    # Build invoice data
    invoice_data = {
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.created_at,
        "due_date": invoice.due_date,
        "period_start": invoice.period_start,
        "period_end": invoice.period_end,
        "customer": {
            "name": invoice.billing_name,
            "email": invoice.billing_email,
            "address": invoice.billing_address,
        },
        "line_items": invoice.line_items or [],
        "subtotal": invoice.subtotal,
        "tax": invoice.tax,
        "total": invoice.total,
        "amount_paid": invoice.amount_paid,
        "amount_due": invoice.amount_due,
        "notes": invoice.notes,
    }
    
    return invoice_pdf_generator.generate(invoice_data, transactions)
