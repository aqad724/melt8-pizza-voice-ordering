# Replit.md

## Overview

This is a real-time AI voice application that integrates Twilio's voice services with OpenAI's Realtime API to create an interactive voice assistant. The system allows users to make phone calls and have natural conversations with an AI that can process speech in real-time, providing immediate voice responses through Twilio's telephony infrastructure.

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Backend Architecture
- **Framework**: FastAPI-based Python web application providing REST and WebSocket endpoints
- **Asynchronous Design**: Built with async/await patterns to handle concurrent voice streams and WebSocket connections
- **Real-time Communication**: WebSocket support for bidirectional audio streaming between services

### Voice Processing Pipeline
- **Voice Input**: Audio received through Twilio's telephony infrastructure via phone calls
- **Real-time Processing**: OpenAI's Realtime API handles immediate audio-to-text and text-to-audio conversion
- **Voice Output**: Processed audio streamed back through Twilio to the caller
- **Voice Configuration**: Uses "alloy" voice model with configurable prompt system

### Configuration Management
- **Environment Variables**: Secure API key management using python-dotenv
- **Prompt System**: Configurable prompt ID and version system for customizing AI behavior
- **Event Logging**: Selective monitoring of specific event types including response completion, rate limits, audio buffer events, and session management

### API Structure
- **REST Endpoints**: FastAPI routes for HTTP request handling and webhooks
- **WebSocket Endpoints**: Real-time connections for audio data streaming
- **Twilio Integration**: Voice webhook handling for call management and TwiML response generation

## External Dependencies

### Core Services
- **OpenAI Realtime API**: Primary AI processing engine for voice-to-voice conversations
- **Twilio Voice API**: Telephony infrastructure for handling phone calls and audio streaming

### Python Libraries
- **fastapi**: Web framework for building the API server
- **websockets**: WebSocket client/server implementation for real-time communication
- **twilio**: Official Twilio SDK for voice response handling and TwiML generation
- **python-dotenv**: Environment variable management for secure configuration
- **uvicorn**: ASGI server for running the FastAPI application

### Infrastructure Requirements
- **WebSocket Support**: Required for real-time audio streaming between services
- **HTTPS Endpoints**: Necessary for Twilio webhook integration
- **Environment Variables**: OPENAI_API_KEY must be configured in deployment environment