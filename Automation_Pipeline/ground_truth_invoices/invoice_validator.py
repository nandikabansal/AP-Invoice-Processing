import json
import os
import difflib
from typing import Dict, Any, Optional, List, Tuple
from pymongo import MongoClient
from datetime import datetime
import time

class InvoiceValidator:
    """Validate invoices and calculate confidence scores for automatic approval"""
    
    def __init__(self, mongo_uri: str, confidence_threshold: float = 0.75):
        """
        Initialize the validator with MongoDB connection and confidence threshold
        
        Args:
            mongo_uri: MongoDB connection string
            confidence_threshold: Minimum confidence score for auto-approval (0-1)
        """
        self.mongo_uri = mongo_uri
        self.confidence_threshold = confidence_threshold
        self.db = self.connect_to_mongodb()
        
    def connect_to_mongodb(self):
        """Connect to MongoDB and return database object"""
        try:
            client = MongoClient(self.mongo_uri)
            return client["invoice_automation"]
        except Exception as e:
            print(f"Failed to connect to MongoDB: {e}")
            raise
    
    def get_historical_invoices(self, vendor_name: str = None) -> List[Dict]:
        """Retrieve historical invoices from MongoDB for comparison"""
        query = {"status": "Approved"}
        if vendor_name:
            query["invoice_header.vendor_name"] = vendor_name
            
        return list(self.db.invoices.find(query, limit=100))
    
    def calculate_field_similarity(self, value1: Any, value2: Any) -> float:
        """
        Calculate similarity between two field values
        Returns a score between 0.0 and 1.0
        """
        if value1 is None and value2 is None:
            return 1.0
        if value1 is None or value2 is None:
            return 0.0
            
        str1 = str(value1).lower().strip()
        str2 = str(value2).lower().strip()
        
        if str1 == str2:
            return 1.0
        
        return difflib.SequenceMatcher(None, str1, str2).ratio()
    
    def validate_invoice_structure(self, invoice: Dict) -> float:
        """Validate the basic structure of the invoice document"""
        required_header_fields = [
            "invoice_num", "invoice_date", "vendor_name", 
            "invoice_amount", "currency_code"
        ]
        required_line_fields = [
            "description", "quantity", "unit_price", "line_amount"
        ]
        
        # Check header fields
        header = invoice.get("invoice_header", {})
        header_score = sum(
            1 for field in required_header_fields if field in header
        ) / len(required_header_fields)
        
        # Check line items
        lines = invoice.get("invoice_lines", [])
        if not lines:
            return header_score * 0.7  # No lines means max 70% score
        
        line_scores = []
        for line in lines:
            line_score = sum(
                1 for field in required_line_fields if field in line
            ) / len(required_line_fields)
            line_scores.append(line_score)
        
        avg_line_score = sum(line_scores) / len(line_scores)
        return (header_score * 0.5) + (avg_line_score * 0.5)
    
    def validate_amount_calculations(self, invoice: Dict) -> float:
        """Validate that line amounts add up to invoice total"""
        lines = invoice.get("invoice_lines", [])
        if not lines:
            return 0.0
            
        header = invoice.get("invoice_header", {})
        invoice_amount = header.get("invoice_amount")
        if invoice_amount is None:
            return 0.0
            
        calculated_total = sum(
            line.get("line_amount", 0) for line in lines
        )
        
        if abs(calculated_total - invoice_amount) < 0.01:  # 1% tolerance
            return 1.0
        return 0.8 if abs(calculated_total - invoice_amount) < (0.05 * invoice_amount) else 0.0
    
    def compare_with_historical(self, invoice: Dict) -> float:
        """Compare with historical invoices from the same vendor"""
        vendor_name = invoice.get("invoice_header", {}).get("vendor_name")
        if not vendor_name:
            return 0.5  # Neutral score if no vendor info
            
        historical_invoices = self.get_historical_invoices(vendor_name)
        if not historical_invoices:
            return 0.6  # Slightly positive if no history
            
        # Compare with most recent 5 invoices from this vendor
        recent_invoices = sorted(
            historical_invoices,
            key=lambda x: x.get("invoice_header", {}).get("invoice_date", ""),
            reverse=True
        )[:5]
        
        similarity_scores = []
        for hist_invoice in recent_invoices:
            # Compare vendor details
            vendor_similarity = self.calculate_field_similarity(
                invoice.get("invoice_header", {}).get("vendor_name"),
                hist_invoice.get("invoice_header", {}).get("vendor_name")
            )
            
            # Compare amounts (normalized)
            amount1 = invoice.get("invoice_header", {}).get("invoice_amount", 0)
            amount2 = hist_invoice.get("invoice_header", {}).get("invoice_amount", 0)
            if amount1 > 0 and amount2 > 0:
                amount_similarity = 1.0 - min(1.0, abs(amount1 - amount2) / max(amount1, amount2))
            else:
                amount_similarity = 0.5
                
            # Compare line item structures
            line_items1 = invoice.get("invoice_lines", [])
            line_items2 = hist_invoice.get("invoice_lines", [])
            
            if line_items1 and line_items2:
                # Compare number of line items
                count_similarity = min(len(line_items1), len(line_items2)) / max(len(line_items1), len(line_items2))
                
                # Compare field presence
                fields1 = set(line_items1[0].keys()) if line_items1 else set()
                fields2 = set(line_items2[0].keys()) if line_items2 else set()
                field_similarity = len(fields1.intersection(fields2)) / len(fields1.union(fields2)) if fields1 or fields2 else 0.0
                
                line_similarity = (count_similarity + field_similarity) / 2
            else:
                line_similarity = 0.5
                
            overall_similarity = (vendor_similarity * 0.4) + (amount_similarity * 0.3) + (line_similarity * 0.3)
            similarity_scores.append(overall_similarity)
        
        return max(similarity_scores) if similarity_scores else 0.5
    
    def calculate_confidence_score(self, invoice: Dict) -> float:
        """Calculate overall confidence score for the invoice"""
        structure_score = self.validate_invoice_structure(invoice)
        calculation_score = self.validate_amount_calculations(invoice)
        historical_score = self.compare_with_historical(invoice)
        
        return (structure_score * 0.4) + (calculation_score * 0.3) + (historical_score * 0.3)
    
    def process_new_invoice(self, invoice_id: str) -> Dict:
        """Process a new invoice and potentially auto-approve it"""
        invoice = self.db.invoices.find_one({"_id": invoice_id})
        if not invoice:
            return {"error": "Invoice not found"}
            
        confidence = self.calculate_confidence_score(invoice)
        result = {
            "invoice_id": str(invoice_id),
            "invoice_number": invoice.get("invoice_header", {}).get("invoice_num"),
            "vendor": invoice.get("invoice_header", {}).get("vendor_name"),
            "confidence_score": confidence,
            "auto_approved": False
        }
        
        if confidence >= self.confidence_threshold:
            # Auto-approve the invoice
            self.db.invoices.update_one(
                {"_id": invoice_id},
                {"$set": {
                    "invoice_header.invoice_status": "Approved",
                    "validation_details": result,
                    "processed_at": datetime.utcnow()
                }}
            )
            result["auto_approved"] = True
        else:
            # Mark for review
            self.db.invoices.update_one(
                {"_id": invoice_id},
                {"$set": {
                    "invoice_header.invoice_status": "requires_review",
                    "validation_details": result,
                    "processed_at": datetime.utcnow()
                }}
            )
        
        return result
    
    def process_pending_invoices(self, batch_size: int = 10):
        """Process all pending invoices in the system"""
        pending_invoices = self.db.invoices.find(
            {"invoice_header.invoice_status": "pending"},
            limit=batch_size
        )
        
        results = []
        for invoice in pending_invoices:
            try:
                result = self.process_new_invoice(invoice["_id"])
                results.append(result)
                time.sleep(0.1)  # Small delay to avoid overwhelming the system
            except Exception as e:
                print(f"Error processing invoice {invoice.get('_id')}: {str(e)}")
                results.append({
                    "invoice_id": str(invoice.get("_id")),
                    "error": str(e)
                })
        
        return results

def main():
    """Example usage of the InvoiceValidator"""
    # Use the same MongoDB URI as your main application
    MONGO_URI = "ADD YOUR MONGO DB PUBLIC URL HERE"
    
    validator = InvoiceValidator(mongo_uri=MONGO_URI, confidence_threshold=0.75)
    
    print("Processing pending invoices...")
    results = validator.process_pending_invoices()
    
    print("\nProcessing Results:")
    print("-" * 40)
    for result in results:
        if "error" in result:
            print(f"Error processing {result.get('invoice_id')}: {result['error']}")
        else:
            status = "APPROVED" if result["auto_approved"] else "NEEDS REVIEW"
            print(f"Invoice {result['invoice_number']} ({result['vendor']})")
            print(f"  Confidence: {result['confidence_score']:.2f} - {status}")
            print()

if __name__ == "__main__":
    main()