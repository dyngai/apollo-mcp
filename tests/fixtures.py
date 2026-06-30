"""Apollo.io API response fixtures (port of src/__tests__/fixtures/apollo-responses.ts)."""

mock_person_search_response = {
    "breadcrumbs": [],
    "partial_results_only": False,
    "disable_eu_prospecting": False,
    "partial_results_limit": 10000,
    "pagination": {
        "page": 1,
        "per_page": 10,
        "total_entries": 145,
        "total_pages": 15,
    },
    "people": [
        {
            "id": "5f7b1234567890abcdef1234",
            "first_name": "John",
            "last_name": "Doe",
            "name": "John Doe",
            "title": "Software Engineer",
            "email": "john.doe@example.com",
            "email_status": "verified",
            "photo_url": "https://example.com/photo.jpg",
            "linkedin_url": "https://linkedin.com/in/johndoe",
            "twitter_url": None,
            "github_url": None,
            "facebook_url": None,
            "city": "San Francisco",
            "state": "California",
            "country": "United States",
            "organization_id": "5f7b9876543210fedcba9876",
            "organization": {
                "id": "5f7b9876543210fedcba9876",
                "name": "Example Corp",
                "website_url": "https://example.com",
                "blog_url": None,
                "angellist_url": None,
                "linkedin_url": "https://linkedin.com/company/example-corp",
                "twitter_url": "https://twitter.com/examplecorp",
                "facebook_url": None,
                "primary_phone": {
                    "number": "+1 415-555-0100",
                    "source": "Account",
                },
                "founded_year": 2010,
                "industry": "Computer Software",
                "keywords": ["saas", "b2b", "enterprise"],
                "estimated_num_employees": 250,
                "short_description": "Example Corp provides software solutions",
            },
        }
    ],
}

mock_company_search_response = {
    "breadcrumbs": [],
    "partial_results_only": False,
    "disable_eu_prospecting": False,
    "partial_results_limit": 10000,
    "pagination": {
        "page": 1,
        "per_page": 10,
        "total_entries": 52,
        "total_pages": 6,
    },
    "organizations": [
        {
            "id": "5f7b9876543210fedcba9876",
            "name": "Example Corp",
            "website_url": "https://example.com",
            "blog_url": None,
            "angellist_url": None,
            "linkedin_url": "https://linkedin.com/company/example-corp",
            "twitter_url": "https://twitter.com/examplecorp",
            "facebook_url": None,
            "primary_phone": {
                "number": "+1 415-555-0100",
                "source": "Account",
            },
            "founded_year": 2010,
            "industry": "Computer Software",
            "keywords": ["saas", "b2b", "enterprise"],
            "estimated_num_employees": 250,
            "short_description": "Example Corp provides software solutions",
            "retail_location_count": 0,
            "raw_address": "123 Main St, San Francisco, CA 94105",
            "city": "San Francisco",
            "state": "California",
            "country": "United States",
            "owned_by_organization_id": None,
            "suborganizations": [],
            "num_suborganizations": 0,
            "seo_description": "Example Corp - Software Solutions",
            "publicly_traded_symbol": None,
            "publicly_traded_exchange": None,
            "logo_url": "https://example.com/logo.png",
            "crunchbase_url": None,
            "primary_domain": "example.com",
            "personas": ["engineering", "saas"],
            "sanitized_phone": "+14155550100",
        }
    ],
}

mock_person_enrich_response = {
    "person": {
        "id": "5f7b1234567890abcdef1234",
        "first_name": "John",
        "last_name": "Doe",
        "name": "John Doe",
        "title": "Software Engineer",
        "email": "john.doe@example.com",
        "email_status": "verified",
        "photo_url": "https://example.com/photo.jpg",
        "linkedin_url": "https://linkedin.com/in/johndoe",
        "employment_history": [
            {
                "id": "emp1",
                "created_at": "2022-01-01T00:00:00.000Z",
                "current": True,
                "degree": None,
                "description": None,
                "emails": None,
                "end_date": None,
                "grade_level": None,
                "kind": None,
                "major": None,
                "organization_id": "5f7b9876543210fedcba9876",
                "organization_name": "Example Corp",
                "raw_address": None,
                "start_date": "2020-01-01",
                "title": "Software Engineer",
                "updated_at": "2023-01-01T00:00:00.000Z",
                "key": "emp_key_1",
            }
        ],
        "state": "California",
        "city": "San Francisco",
        "country": "United States",
        "organization_id": "5f7b9876543210fedcba9876",
        "organization": {
            "id": "5f7b9876543210fedcba9876",
            "name": "Example Corp",
            "website_url": "https://example.com",
            "primary_domain": "example.com",
        },
    }
}

mock_company_enrich_response = {
    "organization": {
        "id": "5f7b9876543210fedcba9876",
        "name": "Example Corp",
        "website_url": "https://example.com",
        "primary_domain": "example.com",
        "industry": "Computer Software",
        "estimated_num_employees": 250,
    }
}

mock_bulk_enrich_people_response = {
    "matches": [
        {
            "id": "5f7b1234567890abcdef1234",
            "first_name": "John",
            "last_name": "Doe",
            "email": "john.doe@example.com",
            "title": "Software Engineer",
            "organization_name": "Example Corp",
        },
        {
            "id": "5f7b1234567890abcdef5678",
            "first_name": "Jane",
            "last_name": "Smith",
            "email": "jane.smith@example.com",
            "title": "Product Manager",
            "organization_name": "Example Corp",
        },
    ]
}

mock_bulk_enrich_organizations_response = {
    "organizations": [
        {
            "id": "5f7b9876543210fedcba9876",
            "name": "Example Corp",
            "website_url": "https://example.com",
            "primary_domain": "example.com",
            "industry": "Computer Software",
            "estimated_num_employees": 250,
        },
        {
            "id": "5f7b9876543210fedcba5432",
            "name": "Another Company",
            "website_url": "https://another.com",
            "primary_domain": "another.com",
            "industry": "Internet",
            "estimated_num_employees": 50,
        },
    ]
}

mock_job_postings_response = {
    "job_postings": [
        {
            "id": "job1",
            "title": "Senior Software Engineer",
            "url": "https://example.com/jobs/senior-engineer",
            "posted_at": "2024-01-15T00:00:00.000Z",
            "location": "San Francisco, CA",
            "description": "We are looking for a senior software engineer...",
        },
        {
            "id": "job2",
            "title": "Product Manager",
            "url": "https://example.com/jobs/product-manager",
            "posted_at": "2024-01-20T00:00:00.000Z",
            "location": "Remote",
            "description": "Join our product team...",
        },
    ]
}

mock_news_articles_response = {
    "news_articles": [
        {
            "id": "news1",
            "title": "Example Corp Raises $5M in Series A",
            "url": "https://techcrunch.com/example-corp-funding",
            "published_date": "2024-01-10",
            "snippet": "Example Corp announced today that it has raised $5M...",
            "source": "TechCrunch",
        },
        {
            "id": "news2",
            "title": "Example Corp Launches New Product",
            "url": "https://venturebeat.com/example-corp-product",
            "published_date": "2024-02-01",
            "snippet": "Example Corp today unveiled its latest product...",
            "source": "VentureBeat",
        },
    ],
    "pagination": {
        "page": 1,
        "per_page": 10,
        "total_entries": 25,
        "total_pages": 3,
    },
}
