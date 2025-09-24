#!/usr/bin/env python3
"""
Test Data Validation Script

This script validates that the test data was created correctly and provides
a summary of the generated data for verification.

Run after populate_test_data.py to confirm everything worked correctly.
"""

import os
import sys

# Add src to path so we can import database modules if needed
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError as e:
    print(f"Missing required packages. Please install with:")
    print("pip install psycopg2-binary")
    sys.exit(1)

# Database configuration
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': os.getenv('DB_PORT', 5432),
    'database': os.getenv('DB_NAME', 'code4meV2'),
    'user': os.getenv('DB_USER', 'postgres'),
    'password': os.getenv('DB_PASSWORD', 'postgres')
}


class TestDataValidator:
    def __init__(self):
        self.conn = None
        self.cursor = None
        
    def connect(self):
        """Connect to the database"""
        try:
            self.conn = psycopg2.connect(**DB_CONFIG)
            self.cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            print(f"✅ Connected to database: {DB_CONFIG['database']}")
        except Exception as e:
            print(f"❌ Failed to connect to database: {e}")
            sys.exit(1)

    def close(self):
        """Close database connection"""
        if self.cursor:
            self.cursor.close()
        if self.conn:
            self.conn.close()

    def validate_basic_counts(self):
        """Validate basic record counts"""
        print("\n📊 Validating Basic Record Counts")
        print("=" * 40)
        
        queries = {
            'Users': 'SELECT COUNT(*) as count FROM "user"',
            'Admin Users': 'SELECT COUNT(*) as count FROM "user" WHERE is_admin = true',
            'Configurations': 'SELECT COUNT(*) as count FROM config WHERE config_id > 1',
            'Projects': 'SELECT COUNT(*) as count FROM project',
            'Sessions': 'SELECT COUNT(*) as count FROM session',
            'Meta Queries': 'SELECT COUNT(*) as count FROM meta_query',
            'Completion Queries': 'SELECT COUNT(*) as count FROM completion_query',
            'Chat Queries': 'SELECT COUNT(*) as count FROM chat_query',
            'Generations': 'SELECT COUNT(*) as count FROM had_generation',
            'Studies': 'SELECT COUNT(*) as count FROM study'
        }
        
        for name, query in queries.items():
            self.cursor.execute(query)
            count = self.cursor.fetchone()['count']
            print(f"  {name:20}: {count:6d}")

    def validate_data_integrity(self):
        """Validate data relationships and integrity"""
        print("\n🔍 Validating Data Integrity")
        print("=" * 40)
        
        # Check for orphaned records
        checks = [
            {
                'name': 'Users with invalid configs',
                'query': '''
                    SELECT COUNT(*) as count FROM "user" u
                    LEFT JOIN config c ON u.config_id = c.config_id
                    WHERE c.config_id IS NULL
                '''
            },
            {
                'name': 'Meta queries with missing users',
                'query': '''
                    SELECT COUNT(*) as count FROM meta_query mq
                    LEFT JOIN "user" u ON mq.user_id = u.user_id
                    WHERE u.user_id IS NULL
                '''
            },
            {
                'name': 'Generations with missing queries',
                'query': '''
                    SELECT COUNT(*) as count FROM had_generation hg
                    LEFT JOIN meta_query mq ON hg.meta_query_id = mq.meta_query_id
                    WHERE mq.meta_query_id IS NULL
                '''
            },
            {
                'name': 'Sessions with missing users',
                'query': '''
                    SELECT COUNT(*) as count FROM session s
                    LEFT JOIN "user" u ON s.user_id = u.user_id
                    WHERE s.user_id IS NOT NULL AND u.user_id IS NULL
                '''
            }
        ]
        
        all_good = True
        for check in checks:
            self.cursor.execute(check['query'])
            count = self.cursor.fetchone()['count']
            status = "✅" if count == 0 else "❌"
            if count > 0:
                all_good = False
            print(f"  {status} {check['name']:35}: {count}")
        
        if all_good:
            print("\n  ✅ All integrity checks passed!")

    def show_data_distribution(self):
        """Show distribution of key data points"""
        print("\n📈 Data Distribution Analysis")
        print("=" * 40)
        
        # Query type distribution
        print("\n  Query Types:")
        self.cursor.execute('''
            SELECT query_type, COUNT(*) as count,
                   ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM meta_query), 1) as percentage
            FROM meta_query
            GROUP BY query_type
            ORDER BY count DESC
        ''')
        for row in self.cursor.fetchall():
            print(f"    {row['query_type']:15}: {row['count']:4d} ({row['percentage']:4.1f}%)")

        # Programming languages
        print("\n  Top Programming Languages:")
        self.cursor.execute('''
            SELECT pl.language_name, COUNT(*) as count,
                   ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM meta_query mq
                        JOIN contextual_telemetry ct ON mq.contextual_telemetry_id = ct.contextual_telemetry_id
                        WHERE ct.language_id IS NOT NULL), 1) as percentage
            FROM meta_query mq
            JOIN contextual_telemetry ct ON mq.contextual_telemetry_id = ct.contextual_telemetry_id
            JOIN programming_language pl ON ct.language_id = pl.language_id
            GROUP BY pl.language_name
            ORDER BY count DESC
            LIMIT 8
        ''')
        for row in self.cursor.fetchall():
            print(f"    {row['language_name']:15}: {row['count']:4d} ({row['percentage']:4.1f}%)")

        # Model acceptance rates
        print("\n  Model Acceptance Rates:")
        self.cursor.execute('''
            SELECT mn.model_name,
                   COUNT(*) as total_generations,
                   COUNT(CASE WHEN hg.was_accepted THEN 1 END) as accepted,
                   ROUND(AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) * 100, 1) as acceptance_rate
            FROM had_generation hg
            JOIN model_name mn ON hg.model_id = mn.model_id
            GROUP BY mn.model_name, mn.model_id
            ORDER BY acceptance_rate DESC
        ''')
        for row in self.cursor.fetchall():
            print(f"    {row['model_name'][:30]:30}: {row['accepted']:3d}/{row['total_generations']:3d} ({row['acceptance_rate']:4.1f}%)")

    def show_time_distribution(self):
        """Show time-based data distribution"""
        print("\n⏰ Time Distribution Analysis")
        print("=" * 40)
        
        # Queries by day (last 7 days)
        print("\n  Queries by Day (Last 7 Days):")
        self.cursor.execute('''
            SELECT DATE(timestamp) as query_date,
                   COUNT(*) as query_count
            FROM meta_query
            WHERE timestamp >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY DATE(timestamp)
            ORDER BY query_date DESC
        ''')
        
        for row in self.cursor.fetchall():
            print(f"    {row['query_date']}: {row['query_count']:4d} queries")

        # Activity by hour
        print("\n  Activity by Hour of Day:")
        self.cursor.execute('''
            SELECT EXTRACT(HOUR FROM timestamp) as hour,
                   COUNT(*) as query_count
            FROM meta_query
            GROUP BY EXTRACT(HOUR FROM timestamp)
            ORDER BY hour
        ''')
        
        hours_data = {int(row['hour']): row['query_count'] for row in self.cursor.fetchall()}
        
        # Simple ASCII bar chart
        if hours_data:
            max_count = max(hours_data.values())
            for hour in range(24):
                count = hours_data.get(hour, 0)
                bar_length = int((count / max_count) * 20) if max_count > 0 else 0
                bar = "█" * bar_length
                print(f"    {hour:2d}:00 |{bar:<20} {count:3d}")

    def show_study_information(self):
        """Show A/B testing study information"""
        print("\n🔬 A/B Testing Studies")
        print("=" * 40)
        
        # List studies
        self.cursor.execute('''
            SELECT s.study_id, s.name, s.description, s.is_active,
                   s.starts_at, s.ends_at,
                   COUNT(DISTINCT cah.user_id) as assigned_users
            FROM study s
            LEFT JOIN config_assignment_history cah ON s.study_id = cah.study_id
            GROUP BY s.study_id, s.name, s.description, s.is_active, s.starts_at, s.ends_at
            ORDER BY s.starts_at DESC
        ''')
        
        studies = self.cursor.fetchall()
        if studies:
            for study in studies:
                status = "🟢 Active" if study['is_active'] else "🔴 Inactive"
                print(f"\n  📋 {study['name']}")
                print(f"     Status: {status}")
                print(f"     Users: {study['assigned_users']}")
                print(f"     Started: {study['starts_at'].date()}")
                if study['ends_at']:
                    print(f"     Ended: {study['ends_at'].date()}")
                
                # Show config distribution for this study
                self.cursor.execute('''
                    SELECT c.config_id, COUNT(*) as user_count
                    FROM config_assignment_history cah
                    JOIN config c ON cah.assigned_config_id = c.config_id
                    WHERE cah.study_id = %s
                    GROUP BY c.config_id
                    ORDER BY c.config_id
                ''', (study['study_id'],))
                
                config_dist = self.cursor.fetchall()
                if config_dist:
                    print(f"     Config distribution:")
                    for config in config_dist:
                        print(f"       Config {config['config_id']}: {config['user_count']} users")
        else:
            print("  No studies found")

    def show_admin_users(self):
        """Show admin user information"""
        print("\n👑 Admin Users (for testing)")
        print("=" * 40)
        
        self.cursor.execute('''
            SELECT email, name, config_id, verified, joined_at
            FROM "user"
            WHERE is_admin = true
            ORDER BY email
        ''')
        
        admins = self.cursor.fetchall()
        for admin in admins:
            status = "✅ Verified" if admin['verified'] else "⏳ Unverified"
            print(f"  📧 {admin['email']}")
            print(f"     Name: {admin['name']}")
            print(f"     Config ID: {admin['config_id']}")
            print(f"     Status: {status}")
            print(f"     Joined: {admin['joined_at'].date()}")
            print()

    def validate_analytics_readiness(self):
        """Check if data is ready for analytics testing"""
        print("\n🎯 Analytics Readiness Check")
        print("=" * 40)
        
        checks = []
        
        # Check for recent data
        self.cursor.execute('''
            SELECT COUNT(*) as count FROM meta_query
            WHERE timestamp >= CURRENT_DATE - INTERVAL '7 days'
        ''')
        recent_queries = self.cursor.fetchone()['count']
        checks.append(("Recent queries (last 7 days)", recent_queries, recent_queries >= 100))
        
        # Check for model diversity
        self.cursor.execute('''
            SELECT COUNT(DISTINCT model_id) as count FROM had_generation
        ''')
        model_diversity = self.cursor.fetchone()['count']
        checks.append(("Different models used", model_diversity, model_diversity >= 2))
        
        # Check for language diversity
        self.cursor.execute('''
            SELECT COUNT(DISTINCT language_id) as count FROM contextual_telemetry
            WHERE language_id IS NOT NULL
        ''')
        language_diversity = self.cursor.fetchone()['count']
        checks.append(("Programming languages", language_diversity, language_diversity >= 5))
        
        # Check for acceptance rate variation
        self.cursor.execute('''
            SELECT MIN(acceptance_rate) as min_rate, MAX(acceptance_rate) as max_rate
            FROM (
                SELECT AVG(CASE WHEN was_accepted THEN 1.0 ELSE 0.0 END) as acceptance_rate
                FROM had_generation
                GROUP BY model_id
            ) model_rates
        ''')
        rates = self.cursor.fetchone()
        acceptance_variation = (rates['max_rate'] or 0) - (rates['min_rate'] or 0) if rates['min_rate'] is not None else 0
        checks.append(("Acceptance rate variation", f"{acceptance_variation:.2f}", acceptance_variation >= 0.1))
        
        # Check for active studies
        self.cursor.execute('''
            SELECT COUNT(*) as count FROM study WHERE is_active = true
        ''')
        active_studies = self.cursor.fetchone()['count']
        checks.append(("Active A/B studies", active_studies, active_studies >= 1))
        
        # Display results
        all_passed = True
        for check_name, value, passed in checks:
            status = "✅" if passed else "⚠️"
            if not passed:
                all_passed = False
            print(f"  {status} {check_name:25}: {value}")
        
        print()
        if all_passed:
            print("  🎉 All checks passed! Your analytics platform is ready for testing.")
        else:
            print("  ⚠️  Some checks failed. You might want to regenerate test data.")

def main():
    """Main execution function"""
    print("🔍 Code4Me Analytics Test Data Validator")
    print("=" * 50)
    
    validator = TestDataValidator()
    
    try:
        validator.connect()
        
        validator.validate_basic_counts()
        validator.validate_data_integrity()
        validator.show_data_distribution()
        validator.show_time_distribution()
        validator.show_study_information()
        validator.show_admin_users()
        validator.validate_analytics_readiness()
        
        print(f"\n🎯 Next Steps:")
        print(f"  1. Start your backend server: python src/main.py")
        print(f"  2. Login as an admin user to see all analytics")
        print(f"  3. Test different time windows and filters")
        print(f"  4. Explore A/B testing study results")
        
    except Exception as e:
        print(f"❌ Error during validation: {e}")
        import traceback
        traceback.print_exc()
    finally:
        validator.close()


if __name__ == "__main__":
    main()

