import CryptoJS from "crypto-js";

/**
 * Hash a password with a salt
 *
 * @param {string} password - The password to hash
 * @returns {string} - The hashed password
 */
export const hashPassword = (password) => {
  return CryptoJS.SHA256(password).toString(CryptoJS.enc.Hex);
};
/**
 * Make an API request to create a new user
 *
 * @param {Object} userData - User data including name, email, password, and optional googleCredential
 * @returns {Promise} - Promise that resolves with the API response
 */
export const createUser = async (userData) => {
  const { name, email, password, googleCredential } = userData;
  try {
    // Prepare the request body
    const requestBody = {
      name,
      email,
      password,
      config_id: 1, // Default config ID - you may want to make this configurable
    };

    // If we have a Google credential, include it in the request
    if (googleCredential) {
      requestBody.token = googleCredential;
      requestBody.provider = "google";
    }

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/user/create/`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include", // Include cookies in the request
        body: JSON.stringify(requestBody),
      },
    );
    const responseBody = await response.json();
    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["message"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        message: responseBody["message"],
        user_id: responseBody["user_id"],
      };
    }
  } catch (error) {
    console.error("Error creating user:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Authenticate a user with email and password
 *
 * @param {Object} credentials - User credentials (email, password)
 * @returns {Promise} - Promise that resolves with the API response
 */
export const authenticateUser = async (credentials) => {
  const { email, password } = credentials;

  try {
    console.log("Authenticating user:", email);

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/user/authenticate/`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include", // Include cookies for auth token
        body: JSON.stringify({ email: email, password: password }),
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["message"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        message: responseBody["message"],
        user: responseBody["user"],
        config: responseBody["config"],
      };
    }
  } catch (error) {
    console.error("Authentication error:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Authenticate a user with OAuth
 *
 * @param {Object} oauthData - OAuth data (provider, token)
 * @returns {Promise} - Promise that resolves with the API response
 */
export const authenticateWithOAuth = async (oauthData) => {
  const { provider, token } = oauthData;

  try {
    console.log(`Authenticating user with ${provider} OAuth`);
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/user/authenticate/`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include", // Include cookies for auth token
        body: JSON.stringify({ token: token, provider: provider }),
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["message"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        message: responseBody["message"],
        user: responseBody["user"],
        config: responseBody["config"],
      };
    }
  } catch (error) {
    console.error(`${provider} OAuth authentication error:`, error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Generate a session token
 *
 * @returns {string} - A session token
 */
export const generateSessionToken = () => {
  return (
    Math.random().toString(36).substring(2, 15) +
    Math.random().toString(36).substring(2, 15)
  );
};

/**
 * Get current user information from auth token
 *
 * @returns {Promise} - Promise that resolves with user data
 */
export const getCurrentUser = async () => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/user/get/`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include", // Include auth token cookie
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["message"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        user: responseBody["user"],
        config: responseBody["config"],
      };
    }
  } catch (error) {
    console.error("Error getting current user:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Check if current user is verified
 *
 * @returns {Promise} - Promise that resolves with verification status
 */
export const checkVerificationStatus = async () => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/user/verify/check/`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include", // Include auth token cookie
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["message"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        verified: responseBody["user_is_verified"],
      };
    }
  } catch (error) {
    console.error("Error checking verification status:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Resend verification email
 *
 * @returns {Promise} - Promise that resolves with API response
 */
export const resendVerificationEmail = async () => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/user/verify/resend/`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include", // Include auth token cookie
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["message"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        message: responseBody["message"] || "Verification email sent successfully",
      };
    }
  } catch (error) {
    console.error("Error resending verification email:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Get Grafana visualization data
 *
 * @returns {Promise} - Promise that resolves with visualization data
 */
export const getVisualizationData = async () => {
  try {
    // In the end website, this would fetch data from a Grafana API
    // For now, we'll return mock data
    // TODO: remove this and replace with actual API call

    await new Promise((resolve) => setTimeout(resolve, 1000));

    return {
      ok: true,
      data: {
        systemMetrics: generateRandomData(30),
        performanceAnalytics: generateRandomData(30),
        userActivity: generateRandomData(30),
        resourceUtilization: generateRandomData(30),
      },
    };
  } catch (error) {
    console.error("Error fetching visualization data:", error);
    return {
      ok: false,
      error: "Failed to load visualization data",
    };
  }
};

/**
 * Generate random data for visualizations
 *
 * @param {number} points - Number of data points to generate
 * @returns {Array} - Array of data points
 */
const generateRandomData = (points) => {
  return Array.from({ length: points }, (_, i) => ({
    timestamp: Date.now() - (points - i) * 3600000, // hourly data points
    value: Math.floor(Math.random() * 100),
  }));
};

// ===== ANALYTICS API FUNCTIONS =====

/**
 * Get dashboard overview data
 *
 * @param {string} timeWindow - Time window (1d, 7d, 30d)
 * @returns {Promise} - Promise that resolves with overview data
 */
export const getDashboardOverview = async (timeWindow = "7d") => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/overview/dashboard?time_window=${timeWindow}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include", // Include auth token cookie
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching dashboard overview:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Get activity timeline data
 *
 * @param {string} timeWindow - Time window (1h, 6h, 24h, 7d)
 * @param {string} granularity - Data granularity (5m, 15m, 1h, 1d)
 * @returns {Promise} - Promise that resolves with timeline data
 */
export const getActivityTimeline = async (timeWindow = "24h", granularity = "1h") => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/overview/activity-timeline?time_window=${timeWindow}&granularity=${granularity}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching activity timeline:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Get queries over time data
 *
 * @param {Object} params - Query parameters
 * @returns {Promise} - Promise that resolves with usage data
 */
export const getQueriesOverTime = async (params = {}) => {
  try {
    const queryParams = new URLSearchParams({
      granularity: params.granularity || "1h",
      ...params
    }).toString();

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/usage/queries-over-time?${queryParams}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching queries over time:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Get acceptance rates data
 *
 * @param {Object} params - Query parameters
 * @returns {Promise} - Promise that resolves with acceptance rate data
 */
export const getAcceptanceRates = async (params = {}) => {
  try {
    const queryParams = new URLSearchParams({
      group_by: params.group_by || "model",
      ...params
    }).toString();

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/usage/acceptance-rates?${queryParams}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching acceptance rates:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Get model comparison data
 *
 * @param {Object} params - Query parameters
 * @returns {Promise} - Promise that resolves with model comparison data
 */
export const getModelComparison = async (params = {}) => {
  try {
    const queryParams = new URLSearchParams(params).toString();

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/models/comparison?${queryParams}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching model comparison:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Get user engagement data
 *
 * @param {string} timeWindow - Time window (7d, 30d, 90d)
 * @returns {Promise} - Promise that resolves with engagement data
 */
export const getUserEngagement = async (timeWindow = "30d") => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/overview/user-engagement?time_window=${timeWindow}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching user engagement:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

// ===== ADMIN-ONLY STUDY MANAGEMENT FUNCTIONS =====

/**
 * Get list of studies
 *
 * @param {boolean} includeInactive - Include inactive studies
 * @returns {Promise} - Promise that resolves with studies list
 */
export const getStudies = async (includeInactive = false) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/studies/list?include_inactive=${includeInactive}`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching studies:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Create a new study
 *
 * @param {Object} studyData - Study data
 * @returns {Promise} - Promise that resolves with creation result
 */
export const createStudy = async (studyData) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/studies/create`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
        body: JSON.stringify(studyData),
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error creating study:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Activate a study
 *
 * @param {string} studyId - Study ID
 * @returns {Promise} - Promise that resolves with activation result
 */
export const activateStudy = async (studyId) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/studies/${studyId}/activate`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error activating study:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Get study evaluation results
 *
 * @param {string} studyId - Study ID
 * @returns {Promise} - Promise that resolves with evaluation data
 */
export const getStudyEvaluation = async (studyId) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/studies/${studyId}/evaluation`,
      {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
      },
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        error: responseBody["detail"] || `${response.status}: ${response.statusText}`,
      };
    } else {
      return {
        ok: true,
        data: responseBody,
      };
    }
  } catch (error) {
    console.error("Error fetching study evaluation:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};
