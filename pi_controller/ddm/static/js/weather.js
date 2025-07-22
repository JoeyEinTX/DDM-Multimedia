/**
 * DDM Weather Widget
 * Handles weather data fetching and display for outdoor events
 */

const DDMWeather = {
    // Weather state
    weatherData: null,
    refreshInterval: null,
    
    // Configuration
    config: {
        refreshRate: 10 * 60 * 1000, // 10 minutes
        maxRetries: 3,
        retryDelay: 5000 // 5 seconds
    },
    
    // Initialize weather widget
    init() {
        console.log('🌤️ Initializing DDM Weather...');
        this.loadWeatherData();
        this.startAutoRefresh();
    },
    
    // Load weather data from API
    async loadWeatherData() {
        try {
            const response = await fetch('/api/weather/current');
            const data = await response.json();
            
            if (data.success) {
                this.weatherData = data;
                this.renderWeatherData(data);
            } else {
                this.renderError(data.error || 'Failed to load weather data');
            }
            
        } catch (error) {
            console.error('Weather API error:', error);
            this.renderError('Weather service unavailable');
        }
    },
    
    // Render weather data in the sidebar
    renderWeatherData(data) {
        const weatherContent = document.getElementById('weather-content');
        if (!weatherContent) {
            console.error('Weather content container not found');
            return;
        }
        
        const current = data.current;
        
        // Build weather display HTML
        let html = `
            <div class="weather-current">
                <div>
                    <div class="weather-temp">${current.temperature}°F</div>
                    <div class="weather-condition">${current.description}</div>
                </div>
                <div class="text-end">
                    <div style="font-size: 0.9rem; color: var(--ddm-dark-text-muted);">
                        Feels ${current.feels_like}°
                    </div>
                </div>
            </div>
            
            <div class="weather-details">
                <div class="weather-detail">
                    💨 ${current.wind_speed} mph
                </div>
                <div class="weather-detail">
                    💧 ${current.humidity}%
                </div>
                <div class="weather-detail">
                    👁️ ${current.visibility.toFixed(1)} mi
                </div>
                <div class="weather-detail">
                    📊 ${current.pressure} mb
                </div>
            </div>
        `;
        
        // Add hourly forecast if available
        this.loadHourlyForecast().then(hourlyData => {
            if (hourlyData && hourlyData.success && hourlyData.hourly.length > 0) {
                html += `
                    <div class="weather-hourly">
                        <div style="font-size: 0.9rem; font-weight: 500; margin-bottom: 0.5rem; color: var(--ddm-teal-light);">
                            Next 6 Hours
                        </div>
                `;
                
                hourlyData.hourly.slice(0, 6).forEach(hour => {
                    const rainText = hour.rain_probability > 10 ? ` ☔ ${hour.rain_probability}%` : '';
                    html += `
                        <div class="weather-hour">
                            <span>${hour.time}</span>
                            <span>
                                <span class="weather-hour-temp">${hour.temperature}°</span>
                                ${rainText}
                            </span>
                        </div>
                    `;
                });
                
                html += '</div>';
            }
            
            // Add update timestamp
            html += `
                <div style="text-align: center; margin-top: 0.75rem; font-size: 0.75rem; color: var(--ddm-dark-text-muted);">
                    Updated: ${data.updated}
                </div>
            `;
            
            weatherContent.innerHTML = html;
        });
        
        // Set initial content without hourly data
        weatherContent.innerHTML = html;
    },
    
    // Load hourly forecast data
    async loadHourlyForecast() {
        try {
            const response = await fetch('/api/weather/hourly');
            return await response.json();
        } catch (error) {
            console.error('Hourly forecast error:', error);
            return null;
        }
    },
    
    // Render error state
    renderError(message) {
        const weatherContent = document.getElementById('weather-content');
        if (!weatherContent) return;
        
        weatherContent.innerHTML = `
            <div class="weather-error">
                ⚠️ ${message}
            </div>
            <div style="text-align: center; margin-top: 0.5rem;">
                <button class="btn btn-sm btn-outline-light" onclick="DDMWeather.loadWeatherData()">
                    🔄 Retry
                </button>
            </div>
        `;
    },
    
    // Start auto-refresh timer
    startAutoRefresh() {
        if (this.refreshInterval) {
            clearInterval(this.refreshInterval);
        }
        
        this.refreshInterval = setInterval(() => {
            console.log('🌤️ Auto-refreshing weather data...');
            this.loadWeatherData();
        }, this.config.refreshRate);
    },
    
    // Stop auto-refresh
    stopAutoRefresh() {
        if (this.refreshInterval) {
            clearInterval(this.refreshInterval);
            this.refreshInterval = null;
        }
    },
    
    // Manual refresh
    refresh() {
        console.log('🌤️ Manual weather refresh...');
        this.loadWeatherData();
    },
    
    // Get weather summary for other components
    getWeatherSummary() {
        if (!this.weatherData || !this.weatherData.current) {
            return 'Weather unavailable';
        }
        
        const current = this.weatherData.current;
        return `${current.temperature}°F, ${current.description}`;
    },
    
    // Check if weather conditions are suitable for outdoor events
    isEventWeatherSafe() {
        if (!this.weatherData || !this.weatherData.current) {
            return { safe: false, reason: 'Weather data unavailable' };
        }
        
        const current = this.weatherData.current;
        
        // Check for dangerous conditions
        const dangerous = ['thunderstorm', 'heavy rain', 'snow', 'blizzard', 'tornado'];
        const description = current.description.toLowerCase();
        
        for (const condition of dangerous) {
            if (description.includes(condition)) {
                return { 
                    safe: false, 
                    reason: `Dangerous weather: ${current.description}` 
                };
            }
        }
        
        // Check wind conditions (dangerous for outdoor events)
        if (current.wind_speed > 25) {
            return { 
                safe: false, 
                reason: `High winds: ${current.wind_speed} mph` 
            };
        }
        
        // Check temperature extremes
        if (current.temperature < 32 || current.temperature > 100) {
            return { 
                safe: false, 
                reason: `Extreme temperature: ${current.temperature}°F` 
            };
        }
        
        return { safe: true, reason: 'Weather conditions are suitable' };
    }
};

// Auto-initialize when DOM is ready
document.addEventListener('DOMContentLoaded', function () {
    // Delay initialization to ensure all other dashboard components are ready
    setTimeout(() => {
        DDMWeather.init();
    }, 1000);
});

// Make globally available
window.DDMWeather = DDMWeather;
