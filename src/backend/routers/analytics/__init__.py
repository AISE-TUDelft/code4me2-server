"""
Analytics router module for data analysis endpoints.

This module provides comprehensive analytics functionality for:
- Usage metrics and time-series analysis
- Model performance and quality metrics 
- A/B testing and configuration analysis
- User behavior and engagement analytics

All endpoints enforce proper access controls:
- Non-admin users can only see their own data
- Admin users can access all data and cross-user analytics
"""

from fastapi import APIRouter
from . import usage, models, calibration, studies, overview

router = APIRouter()

# Include all analytics sub-routers
router.include_router(usage.router, prefix="/usage", tags=["Usage Analytics"])
router.include_router(models.router, prefix="/models", tags=["Model Analytics"]) 
router.include_router(calibration.router, prefix="/calibration", tags=["Model Calibration"])
router.include_router(studies.router, prefix="/studies", tags=["A/B Testing"])
router.include_router(overview.router, prefix="/overview", tags=["Dashboard Overview"])

