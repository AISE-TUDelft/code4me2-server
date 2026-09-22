import CryptoJS from "crypto-js";

// Build backend base URL with optional port. Default host points to production API.
const BACKEND_HOST = process.env.REACT_APP_BACKEND_HOST || "https://api.code4me.me";
const BACKEND_PORT = Number(process.env.REACT_APP_BACKEND_PORT || 0);
export const BASE_URL = (() => {
  const host = BACKEND_HOST.replace(/\/$/, "");
  return BACKEND_PORT > 0 ? `${host}:${BACKEND_PORT}` : host;
})();

// Intercept fetch calls globally to normalize backend URLs (host/port + trailing slash)
const __origFetch = typeof window !== "undefined" ? window.fetch : undefined;
if (__origFetch) {
  // Wrap only once per module load
  const PREFIX = `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}`;
  window.fetch = (input, init) => {
    try {
      if (typeof input === "string") {
        let urlStr = input;
        // Normalize host + optional port to computed BASE_URL
        if (PREFIX && urlStr.startsWith(PREFIX)) {
          urlStr = BASE_URL + urlStr.substring(PREFIX.length);
        }
        // If URL appears absolute, normalize trailing slash for /api/* paths
        try {
          const u = new URL(urlStr);
          if (u.pathname.startsWith("/api/") && u.pathname !== "/api" && u.pathname.endsWith("/")) {
            // Strip only trailing slashes to match FastAPI canonical paths and avoid 307 redirects
            u.pathname = u.pathname.replace(/\/+$/, "");
            urlStr = u.toString();
          }
        } catch (_) {
          // not an absolute URL (e.g., relative path). Leave as-is.
        }
        input = urlStr;
      }
    } catch (e) {
      // no-op
    }
    return __origFetch(input, init);
  };
}

// Safely parse a fetch Response into JSON if possible; otherwise return a fallback
const parseResponseSafe = async (response) => {
  try {
    const clone = response.clone();
    const ct = clone.headers.get("content-type") || "";
    if (ct.toLowerCase().includes("application/json")) {
      return await clone.json();
    }
  } catch (_) {
    // ignore
  }
  try {
    const text = await response.clone().text();
    if (text && text.trim()) {
      return { message: text };
    }
  } catch (_) {
    // ignore
  }
  return null;
};

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
    const responseBody = await parseResponseSafe(response);

    if (!response.ok) {
      const errMsg = (responseBody && (responseBody["message"] || responseBody["detail"])) || `${response.status}: ${response.statusText}`;
      return {
        ok: false,
        error: errMsg,
      };
    } else {
      if (!responseBody || typeof responseBody !== "object") {
        return { ok: false, error: "Empty or invalid server response." };
      }
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
    const responseBody = await parseResponseSafe(response);

    if (!response.ok) {
      const errMsg = (responseBody && (responseBody["message"] || responseBody["detail"])) || `${response.status}: ${response.statusText}`;
      return {
        ok: false,
        error: errMsg,
      };
    } else {
      if (!responseBody || typeof responseBody !== "object") {
        return { ok: false, error: "Empty or invalid server response." };
      }
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
        message: responseBody["message"] || "Verification email request queued",
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

const getAgentAnalytics = async (path, fallbackError) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/agents/${path}`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body };
  } catch (error) {
    console.error("Error fetching agent analytics:", error);
    return { ok: false, error: fallbackError };
  }
};

export const getAgentOverview = (params = {}) =>
  getAgentAnalytics(`overview?${new URLSearchParams(params)}`, "An unexpected error occurred. Please try again.");

export const getAgentRunDetail = (taskId) =>
  getAgentAnalytics(`runs/${encodeURIComponent(taskId)}`, "Failed to load agent run detail");

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

/**
 * Get study details (assignments and metadata)
 *
 * @param {string} studyId - Study ID
 * @returns {Promise}
 */
export const getStudyDetails = async (studyId) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/studies/${studyId}/details`,
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
    console.error("Error fetching study details:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Deactivate a study
 *
 * @param {string} studyId - Study ID
 * @returns {Promise}
 */
export const deactivateStudy = async (studyId) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/studies/${studyId}/deactivate`,
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
    console.error("Error deactivating study:", error);
    return {
      ok: false,
      error: "An unexpected error occurred. Please try again.",
    };
  }
};

/**
 * Logout the current user by deactivating their session
 *
 * @returns {Promise} - Promise that resolves with logout result
 */
export const logoutUser = async () => {
  console.log("Starting logout API call...");
  
  try {
    const url = `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/session/deactivate/`;
    console.log("Logout URL:", url);
    console.log("Cookies before logout:", document.cookie);
    
    const response = await fetch(url, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
      },
      credentials: "include", // Include cookies for auth token
    });
    
    console.log("Logout response status:", response.status);
    console.log("Logout response headers:", response.headers);
    
    // Parse response if available
    let responseBody = {};
    try {
      responseBody = await response.json();
      console.log("📦 Logout response body:", responseBody);
    } catch (parseError) {
      console.warn("Could not parse logout response:", parseError);
    }

    console.log("Cookies after logout:", document.cookie);

    if (!response.ok) {
      console.warn("Logout API failed with status:", response.status, responseBody);
      return {
        ok: false,
        error: responseBody.message || `HTTP ${response.status}`,
        status: response.status
      };
    }

    console.log("Logout API succeeded");
    return {
      ok: true,
      message: "Session deactivated successfully",
    };
  } catch (error) {
    console.error("Error during logout API call:", error);
    return {
      ok: false,
      error: error.message || "Network error during logout",
    };
  }
};


// ===== MODEL CALIBRATION ANALYTICS FUNCTIONS =====

/**
 * Get reliability diagram data (bins of predicted confidence vs empirical accuracy)
 * @param {Object} params - { start_time, end_time, model_id, bins, user_id }
 */
export const getReliabilityDiagram = async (params = {}) => {
  try {
    const queryParams = new URLSearchParams({
      bins: params.bins || 10,
      ...params,
    }).toString();

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/calibration/reliability-diagram?${queryParams}`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      }
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return { ok: false, error: responseBody["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: responseBody };
  } catch (error) {
    console.error("Error fetching reliability diagram:", error);
    return { ok: false, error: "An unexpected error occurred. Please try again." };
  }
};

/**
 * Get Brier score summary
 * @param {Object} params - { start_time, end_time, model_id, group_by, user_id }
 */
export const getBrierScore = async (params = {}) => {
  try {
    const queryParams = new URLSearchParams({
      group_by: params.group_by || "model",
      ...params,
    }).toString();

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/calibration/brier-score?${queryParams}`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      }
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return { ok: false, error: responseBody["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: responseBody };
  } catch (error) {
    console.error("Error fetching Brier score:", error);
    return { ok: false, error: "An unexpected error occurred. Please try again." };
  }
};

/**
 * Get confidence score distribution (histogram)
 * @param {Object} params - { start_time, end_time, model_id, bins, user_id }
 */
export const getConfidenceDistribution = async (params = {}) => {
  try {
    const queryParams = new URLSearchParams({
      bins: params.bins || 20,
      ...params,
    }).toString();

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/calibration/confidence-distribution?${queryParams}`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      }
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return { ok: false, error: responseBody["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: responseBody };
  } catch (error) {
    console.error("Error fetching confidence distribution:", error);
    return { ok: false, error: "An unexpected error occurred. Please try again." };
  }
};

/**
 * Get calibration summary across models
 * @param {Object} params - { start_time, end_time, user_id }
 */
export const getCalibrationSummary = async (params = {}) => {
  try {
    const queryParams = new URLSearchParams({
      ...params,
    }).toString();

    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/calibration/calibration-summary?${queryParams}`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      }
    );
    const responseBody = await response.json();

    if (!response.ok) {
      return { ok: false, error: responseBody["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: responseBody };
  } catch (error) {
    console.error("Error fetching calibration summary:", error);
    return { ok: false, error: "An unexpected error occurred. Please try again." };
  }
};


// ===== ADMIN-ONLY CONFIG MANAGEMENT FUNCTIONS =====

export const listConfigs = async () => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body.configs || [] };
  } catch (e) {
    console.error("Error listing configs:", e);
    return { ok: false, error: "Failed to load configs" };
  }
};

export const getConfigById = async (configId) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/${configId}`,
      { method: "GET", headers: { "Content-Type": "application/json" }, credentials: "include" },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body };
  } catch (e) {
    console.error("Error getting config:", e);
    return { ok: false, error: "Failed to get config" };
  }
};

export const createConfigApi = async (configData) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ config_data: configData }),
      },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body };
  } catch (e) {
    console.error("Error creating config:", e);
    return { ok: false, error: "Failed to create config" };
  }
};

export const updateConfigApi = async (configId, configData) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/${configId}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ config_data: configData }),
      },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body };
  } catch (e) {
    console.error("Error updating config:", e);
    return { ok: false, error: "Failed to update config" };
  }
};

export const deleteConfigApi = async (configId) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/${configId}`,
      { method: "DELETE", headers: { "Content-Type": "application/json" }, credentials: "include" },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body };
  } catch (e) {
    console.error("Error deleting config:", e);
    return { ok: false, error: "Failed to delete config" };
  }
};

export const getLanguagesMapping = async () => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/languages`,
      { method: "GET", headers: { "Content-Type": "application/json" }, credentials: "include" },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body };
  } catch (e) {
    console.error("Error fetching languages:", e);
    return { ok: false, error: "Failed to load languages" };
  }
};

export const getAvailableModels = async () => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/models`,
      { method: "GET", headers: { "Content-Type": "application/json" }, credentials: "include" },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, data: body.models || [] };
  } catch (e) {
    console.error("Error fetching available models:", e);
    return { ok: false, error: "Failed to load models" };
  }
};

export const validateHuggingFaceModel = async (name) => {
  if (!name || !name.trim()) return { ok: true, exists: false };
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/config/models/validate?name=${encodeURIComponent(name)}`,
      { method: "GET", headers: { "Content-Type": "application/json" }, credentials: "include" },
    );
    const body = await response.json();
    if (!response.ok) {
      return { ok: false, error: body["detail"] || `${response.status}: ${response.statusText}` };
    }
    return { ok: true, exists: !!body.exists, status_code: body.status_code, error: body.error };
  } catch (e) {
    console.error("Error validating HF model:", e);
    return { ok: false, error: "Validation failed" };
  }
};

// ── Agent subsystem ─────────────────────────────────────────────────────────
//
// Backs the admin pages: AgentProfiles (define experiment arms) and
// AgentResults (compare arms). All endpoints are admin-only server-side.
// Assignments are owned by enrollment/study membership; there is no manual
// assignment authoring endpoint (ISSUE-16).

const AGENT_BASE = () =>
  `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/agent`;

// Shared request helper. The hand-rolled fetch blocks above predate it; new
// agent endpoints funnel through here so error handling stays consistent.
//
// Error bodies are normalized so no raw object/array ever reaches JSX:
//   * FastAPI's default 422 `detail` array (`{type,loc,msg,input}`) becomes
//     `errors: [{field, message, code}]` (loc joined with "."), with a readable
//     summary string;
//   * a custom `detail` object `{code, field, message}` becomes one entry;
//   * a plain `detail`/`message` string is used as-is.
// The same helpers back `researchRequest`, so there is one pattern, not two.
const normalizeErrorEntries = (payload) => {
  if (!payload || typeof payload !== "object") return [];
  const detail = payload.detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      if (typeof item === "string") return { field: "", message: item, code: "" };
      const loc = Array.isArray(item.loc) ? item.loc.join(".") : "";
      return {
        field: item.field || loc || "",
        message: item.msg || item.message || "",
        code: item.code || item.type || "",
        severity: item.severity,
      };
    });
  }
  if (Array.isArray(payload.errors)) return payload.errors;
  if (detail && typeof detail === "object") {
    return [
      {
        field: detail.field || "",
        message: detail.message || detail.msg || detail.reason || "",
        code: detail.code || "",
        severity: detail.severity,
      },
    ];
  }
  return [];
};

const normalizeRequestError = (payload, response) => {
  const detail = payload ? payload.detail : undefined;
  const errors = normalizeErrorEntries(payload);
  const detailObject =
    detail && typeof detail === "object" && !Array.isArray(detail) ? detail : null;
  const typedCode =
    (detailObject && detailObject.code) ||
    (payload && payload.code) ||
    errors.find((entry) => entry && entry.code)?.code ||
    "";
  const detailMessage =
    (typeof detail === "string" && detail) ||
    (detailObject &&
      (detailObject.message || detailObject.code || detailObject.reason)) ||
    (payload && payload.message) ||
    "";
  // A typed validation failure must never surface as
  // "422 Unprocessable Entity": summarize the field errors instead.
  const error =
    detailMessage ||
    (errors.length
      ? summarizeValidationErrors(errors)
      : `${response.status}: ${response.statusText}`);
  return { code: typedCode, error, errors, status: response.status };
};

const agentRequest = async (path, { method = "GET", body, label } = {}) => {
  try {
    const response = await fetch(`${AGENT_BASE()}${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    // 204 has no body to parse.
    const payload =
      response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) {
      return { ok: false, ...normalizeRequestError(payload, response) };
    }
    return { ok: true, data: payload, status: response.status };
  } catch (e) {
    console.error(`Error ${label || path}:`, e);
    return {
      ok: false,
      error: `Failed to ${label || "complete request"}`,
      status: null,
      errors: [],
    };
  }
};

export const getAgentProfiles = async () => {
  const result = await agentRequest("/profiles", { label: "load agent profiles" });
  return result.ok ? { ok: true, data: result.data.profiles || [] } : result;
};

export const createAgentProfile = async (profile) =>
  agentRequest("/profiles", {
    method: "POST",
    body: profile,
    label: "create agent profile",
  });

export const updateAgentProfile = async (profileId, profile) =>
  agentRequest(`/profiles/${profileId}`, {
    method: "PUT",
    body: profile,
    label: "update agent profile",
  });

export const deleteAgentProfile = async (profileId) =>
  agentRequest(`/profiles/${profileId}`, {
    method: "DELETE",
    label: "delete agent profile",
  });

// Tool names selectable for a profile. Pass a frameworkVersion to narrow the
// list to the runtime the profile targets.
export const getAgentAvailableTools = async (frameworkVersion) => {
  const query = frameworkVersion
    ? `?framework_version=${encodeURIComponent(frameworkVersion)}`
    : "";
  const result = await agentRequest(`/available-tools${query}`, {
    label: "load available agent tools",
  });
  return result.ok
    ? {
        ok: true,
        data: {
          tools: result.data.tools || [],
          frameworks: result.data.frameworks || [],
        },
      }
    : result;
};

// ── Research control plane ──────────────────────────────────────────────────
//
// Backs the researcher-facing lifecycle pages (ResearchStudies,
// ResearchStudyEditor and ResearchJoin). The backend mounts these under
// /api/research and every endpoint is admin/authorized-researcher only, so they
// reuse the existing cookie-based session like the analytics/admin helpers.
//
// Validation failures from the protocol service are 422 responses whose
// `detail` is a *list* of {code, field, message, severity} entries. Those are
// preserved on `errors` so the UI can render field-level messages instead of
// collapsing them into one opaque string.

const RESEARCH_BASE = () =>
  `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/research`;

const summarizeValidationErrors = (errors) =>
  errors
    .slice(0, 3)
    .map((error) => {
      if (typeof error === "string") return error;
      return `${error.field || error.code || "protocol"}: ${error.message || ""}`.trim();
    })
    .join("; ");

export const researchRequest = async (
  path,
  { method = "GET", body, label } = {},
) => {
  try {
    const response = await fetch(`${RESEARCH_BASE()}${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    // 204 has no body to parse.
    const payload =
      response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) {
      return { ok: false, ...normalizeRequestError(payload, response) };
    }
    return { ok: true, data: payload, status: response.status };
  } catch (e) {
    console.error(`Error ${label || path}:`, e);
    return {
      ok: false,
      error: `Failed to ${label || "complete request"}`,
      status: null,
      errors: [],
    };
  }
};

/**
 * Multipart POST. The browser sets the multipart boundary itself, so no
 * Content-Type header is set here; the same error normalisation as
 * ``researchRequest`` is applied.
 */
export const researchUpload = async (path, formData, { label } = {}) => {
  try {
    const response = await fetch(`${RESEARCH_BASE()}${path}`, {
      method: "POST",
      body: formData,
      credentials: "include",
    });
    // 204 has no body to parse.
    const payload =
      response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) {
      return { ok: false, ...normalizeRequestError(payload, response) };
    }
    return { ok: true, data: payload, status: response.status };
  } catch (e) {
    console.error(`Error ${label || path}:`, e);
    return {
      ok: false,
      error: `Failed to ${label || "complete request"}`,
      status: null,
      errors: [],
    };
  }
};

/**
 * List study identities.
 *
 * NOTE: the research API exposes a study index plus per-study lookups; no
 * draft/revision endpoints exist. The call is made optimistically and a
 * 404/405 is reported as `missing` so the UI can fall back to a study-id
 * entry instead of failing hard.
 */
/**
 * Create a study identity.
 *
 * The server generates the UUID (and grants the caller OWNER so they can
 * author it immediately), which removes the "paste a UUID" step from the UI.
 * The identity starts as a DRAFT study with a study-owned join code.
 */
export const createResearchStudy = async ({ name, description, startsAt, endsAt, telemetryPolicy, sessionPolicy, profileIds } = {}) =>
  researchRequest("/studies", {
    method: "POST",
    body: {
      name,
      description: description ?? null,
      starts_at: startsAt || null,
      ends_at: endsAt || null,
      telemetry_policy: telemetryPolicy || {},
      session_policy: sessionPolicy || {},
      profile_ids: profileIds || [],
    },
    label: "create research study",
  });

export const listResearchStudies = async () => {
  const result = await researchRequest("/studies", {
    label: "load research studies",
  });
  if (result.ok) {
    const data = result.data || {};
    return { ok: true, data: data.studies || data || [] };
  }
  if (result.status === 403) {
    return {
      ok: false,
      forbidden: true,
      error:
        "Your account is not enabled for research. Ask an administrator to enable researcher access.",
    };
  }
  if (result.status === 404 || result.status === 405) {
    return {
      ok: false,
      missing: true,
      error: "The study-listing endpoint is not available on this server yet.",
    };
  }
  return result;
};

export const getResearchStudy = async (studyId) => {
  const result = await researchRequest(
    `/studies/${encodeURIComponent(studyId)}`,
    { label: "load research study" },
  );
  return result.ok ? { ok: true, data: result.data.study || result.data } : result;
};

export const updateResearchStudyMetadata = async (studyId, metadata) =>
  researchRequest(`/studies/${encodeURIComponent(studyId)}/metadata`, {
    method: "PATCH",
    body: metadata,
    label: "update study metadata",
  });

export const stopResearchStudy = async (studyId, actor) =>
  researchRequest(`/studies/${encodeURIComponent(studyId)}/stop`, {
    method: "POST",
    body: { actor: actor ?? null },
    label: "stop research study",
  });

// Clone a stopped study. Supplying profile ids completes the clone through the
// same validated profile freeze as create, so the clone is joinable; omitting
// them leaves an explicitly non-runnable draft (ISSUE-12).
export const cloneResearchStudy = async (studyId, { profileIds } = {}) =>
  researchRequest(`/studies/${encodeURIComponent(studyId)}/clone`, {
    method: "POST",
    ...(Array.isArray(profileIds) && profileIds.length > 0
      ? { body: { profile_ids: profileIds } }
      : {}),
    label: "clone research study",
  });

export const revokeResearchEnrollment = async (studyId, enrollmentId, actor) =>
  researchRequest(
    `/studies/${encodeURIComponent(studyId)}/enrollments/${encodeURIComponent(enrollmentId)}/revoke`,
    {
      method: "POST",
      body: { actor: actor ?? null },
      label: "revoke research enrollment",
    },
  );

export const engageResearchKillSwitch = async (studyId, reason) =>
  researchRequest("/operations/kill-switch", {
    method: "POST",
    body: { scope_kind: "STUDY", scope_id: studyId, reason },
    label: "engage study kill switch",
  });

export const releaseResearchKillSwitch = async (switchId) =>
  researchRequest(`/operations/kill-switch/${encodeURIComponent(switchId)}/release`, {
    method: "POST",
    label: "release study kill switch",
  });

const RESEARCH_ENDPOINT_MISSING = (status) => status === 404 || status === 405;

export const getResearchStudyJoinCode = async (studyId) => {
  const result = await researchRequest(
    `/studies/${encodeURIComponent(studyId)}`,
    { label: "load study join code" },
  );
  if (result.ok) {
    const data = result.data.study || result.data || {};
    return {
      ok: true,
      join_code: data.join_code || "",
      status: data.research_status || "",
      study: data,
    };
  }
  if (RESEARCH_ENDPOINT_MISSING(result.status)) {
    return {
      ok: false,
      missing: true,
      error: "The study join-code endpoint is not available on this server yet.",
    };
  }
  return result;
};

// Resolve a participant join code without redeeming it so the participant can
// review the study and consent text before joining.
export const resolveResearchJoinCode = async (joinCode) => {
  const result = await researchRequest(
    `/join/${encodeURIComponent(joinCode)}`,
    { label: "resolve study join code" },
  );
  if (result.ok) {
    const data = result.data || {};
    const study = data.study || {};
    const consent = data.consent || {};
    return {
      ok: true,
      data: {
        joinCode: data.join_code || joinCode,
        study: {
          studyId: study.study_id || "",
          name: study.name || "",
          description: study.description || "",
          researchStatus: study.research_status || study.researchStatus || "",
          joinability: study.joinability || study.joinability_status || "",
          status: study.status || "",
        },
        policyText: consent.text || consent.consent_text || data.consent_text || "",
        consentText: consent.text || consent.consent_text || data.consent_text || "",
      },
    };
  }
  if (RESEARCH_ENDPOINT_MISSING(result.status)) {
    return {
      ok: false,
      missing: true,
      error:
        "That join code could not be found, or the join service is not available on this server yet.",
    };
  }
  return result;
};

// Redeem a participant join code for the signed-in account. Idempotent
// server-side: re-running reuses the existing enrollment.
export const redeemResearchJoinCode = async (joinCode, acceptConsent) => {
  const result = await researchRequest("/join", {
    method: "POST",
    body: { join_code: joinCode, accept_consent: !!acceptConsent },
    label: "redeem study join code",
  });
  if (result.ok) {
    const data = result.data || {};
    return {
      ok: true,
      data: {
        enrollment_id: data.enrollment_id || "",
        study_id: data.study_id || "",
        assignment_id: data.assignment_id || "",
        agent_profile_id: data.agent_profile_id || "",
        status: data.status || "",
        created: data.created,
        reused: data.reused,
        ...(data.handoff && typeof data.handoff === "object" ? { handoff: data.handoff } : {}),
      },
    };
  }
  if (RESEARCH_ENDPOINT_MISSING(result.status)) {
    // Same ambiguity as resolve: unknown/expired code or an unmounted service.
    return {
      ok: false,
      missing: true,
      error:
        "That join code could not be redeemed, or the join service is not available on this server yet.",
    };
  }
  return result;
};

// The signed-in account's own enrollment projections (study-local, no account
// data). Used by the join page to avoid re-asking for the policy when an active
// enrollment already exists for the code's study.
export const getMyResearchEnrollments = async () => {
  const result = await researchRequest("/participants/me", {
    label: "load my research enrollments",
  });
  if (result.ok) {
    const data = result.data || {};
    return { ok: true, data: data.enrollments || [] };
  }
  if (RESEARCH_ENDPOINT_MISSING(result.status)) {
    return {
      ok: false,
      missing: true,
      error:
        "Your enrollment status is not available on this server yet.",
    };
  }
  return result;
};

// Study-owner-scoped participant coverage (ISSUE-13): study-local participant
// codes, frozen assignments and session/event counts only; no personal
// timelines. The endpoint is owner/admin-only, so a 403 is reported as
// `forbidden` for the UI to show a permission notice.
export const getStudyParticipantCoverage = async (studyId) => {
  const result = await researchRequest(
    `/operations/participants/coverage?study_id=${encodeURIComponent(studyId)}`,
    { label: "load study participant coverage" },
  );
  if (result.ok) {
    const data = result.data || {};
    return {
      ok: true,
      data: {
        ...data,
        participants: Array.isArray(data.participants) ? data.participants : [],
      },
    };
  }
  if (result.status === 403) {
    return {
      ok: false,
      forbidden: true,
      code: result.code || "FORBIDDEN",
      status: 403,
      error:
        "Only the study owner or an administrator can view study participant coverage.",
    };
  }
  if (RESEARCH_ENDPOINT_MISSING(result.status)) {
    return {
      ok: false,
      missing: true,
      error: "Study participant coverage is not available on this server yet.",
    };
  }
  return result;
};

// The researcher-readable release catalogue (ISSUE-11): registered releases
// independent of existing profiles, so a fresh install can author its first
// profile. Read-only and non-secret; importing and qualifying releases remains
// admin-only under /agents/releases.
export const getReleaseCatalogue = async () => {
  const result = await researchRequest("/agents/release-catalogue", {
    label: "load release catalogue",
  });
  return result.ok ? { ok: true, data: result.data.releases || [] } : result;
};

// Derived, read-only distribution views. A profile IS a distribution; every
// entry carries the resolved `release_id`/`release_version`, the server-derived
// `verified` flag and the release's `supported_platforms`. Readable by any
// authenticated user and exposes no secret, so a researcher can pick exactly one
// distribution without ever seeing a release id or digest as an input.
export const getAgentDistributions = async () => {
  const result = await researchRequest("/agents/distributions", {
    label: "load agent distributions",
  });
  return result.ok
    ? { ok: true, data: result.data.distributions || [] }
    : result;
};

export const getAgentDistribution = async (distributionId) => {
  const result = await researchRequest(
    `/agents/distributions/${encodeURIComponent(distributionId)}`,
    { label: "load agent distribution" },
  );
  return result.ok ? { ok: true, data: result.data.distribution } : result;
};

// Provider connections the caller may select for a profile. Administrators see
// every connection (including endpoint/secret_ref); researchers see only the
// connections granted to them. The response never includes a secret value.
export const getProviderConnections = async () => {
  const result = await researchRequest("/provider-connections", {
    label: "load provider connections",
  });
  return result.ok
    ? { ok: true, data: result.data.connections || [] }
    : result;
};

// One registered release with its full digest-pinned artifact and adapter
// metadata. The list endpoint returns a compact summary, so the release picker
// resolves the selected release to auto-fill version/digest/adapter_version.
export const getAgentRelease = async (releaseId) => {
  const result = await researchRequest(
    `/agents/releases/${encodeURIComponent(releaseId)}`,
    { label: "load agent release" },
  );
  return result.ok
    ? { ok: true, release: result.data.release, model: result.data.model }
    : result;
};

export const getResearchPackages = async (releaseId) => {
  const query = releaseId
    ? `?release_id=${encodeURIComponent(releaseId)}`
    : "";
  const result = await researchRequest(`/packages${query}`, {
    label: "load runtime packages",
  });
  return result.ok ? { ok: true, data: result.data.packages || [] } : result;
};

// Per-arm comparison for a study's agent arms.
export const getStudyAgentEvaluation = async (studyId) => {
  try {
    const response = await fetch(
      `${process.env.REACT_APP_BACKEND_HOST}:${process.env.REACT_APP_BACKEND_PORT}/api/analytics/studies/${studyId}/agent-evaluation`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      },
    );
    const body = await response.json();
    if (!response.ok) {
      return {
        ok: false,
        error: body["detail"] || `${response.status}: ${response.statusText}`,
      };
    }
    return { ok: true, data: body };
  } catch (e) {
    console.error("Error loading agent evaluation:", e);
    return { ok: false, error: "Failed to load agent evaluation" };
  }
};

// ── Administrator panel ─────────────────────────────────────────────────────
//
// Backs the admin-only Dashboard views (AdminResearchers, AdminConnections and
// AdminAgents). Every endpoint is administrator-only server-side. All calls go
// through `researchRequest`, so a typed 422 is normalized into `errors` and
// never reaches JSX as a raw FastAPI detail array.

// Every account (not only enabled researchers), newest first. The route keeps
// its historical `/researchers` name but returns the full account list.
export const listAccounts = async ({ limit = 100 } = {}) => {
  const query = limit ? `?limit=${encodeURIComponent(limit)}` : "";
  const result = await researchRequest(`/researchers${query}`, {
    label: "load accounts",
  });
  return result.ok
    ? { ok: true, data: result.data.researchers || [], status: result.status }
    : result;
};

// Toggle the single can_research flag. The server re-checks admin, so a 403 is
// surfaced as a normal error rather than swallowed.
export const setResearcherEnabled = async (userId, canResearch) =>
  researchRequest(`/researchers/${encodeURIComponent(userId)}`, {
    method: "PUT",
    body: { can_research: !!canResearch },
    label: "update researcher access",
  });

export const createProviderConnection = async (connection) =>
  researchRequest("/provider-connections", {
    method: "POST",
    body: connection,
    label: "create provider connection",
  });

export const updateProviderConnection = async (connectionId, connection) =>
  researchRequest(
    `/provider-connections/${encodeURIComponent(connectionId)}`,
    {
      method: "PUT",
      body: connection,
      label: "update provider connection",
    },
  );

export const deleteProviderConnection = async (connectionId) =>
  researchRequest(
    `/provider-connections/${encodeURIComponent(connectionId)}`,
    { method: "DELETE", label: "delete provider connection" },
  );

// Registered releases (admin view). The list is compact, so the release detail
// endpoint supplies the digest-pinned artifacts and adapter identity.
export const listRegisteredReleases = async (agentId) => {
  const query = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : "";
  const result = await researchRequest(`/agents/releases${query}`, {
    label: "load registered releases",
  });
  return result.ok
    ? { ok: true, data: result.data.releases || [], status: result.status }
    : result;
};

export const getRegisteredRelease = async (releaseId) => {
  const result = await researchRequest(
    `/agents/releases/${encodeURIComponent(releaseId)}`,
    { label: "load registered release" },
  );
  return result.ok
    ? { ok: true, release: result.data.release, model: result.data.model }
    : result;
};

// Import a build runtime manifest together with the exact archive bytes it
// declares. The server recomputes every digest and size; a missing, duplicate,
// unexpected or mismatching upload rejects the whole import. There is no
// digest-trusting path.
export const importAgentRelease = async ({ manifest, archives } = {}) => {
  const form = new FormData();
  form.append(
    "manifest",
    typeof manifest === "string" ? manifest : JSON.stringify(manifest ?? {}),
  );
  (archives || []).forEach((file) => {
    if (file) form.append("archives", file, file.name);
  });
  return researchUpload("/agents/releases/import", form, {
    label: "import agent release",
  });
};

export const importAgentReleaseUrl = async ({ manifest_url, archive_urls }) =>
  researchRequest("/agents/releases/import-url", {
    method: "POST", body: { manifest_url, archive_urls }, label: "import release URLs",
  });

export const disableAgentRelease = async (releaseId) =>
  researchRequest(`/agents/releases/${encodeURIComponent(releaseId)}/disable`, {
    method: "POST", label: "disable release",
  });
