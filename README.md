Microsoft Dynamics 365 Business Central
=============

Description

Component allows downloading any endpoint via oData api, supports incremental load.

1. In your Azure tenant where Business Central is deployed, navigate to **App registrations** > **New registration** and register the application named "Keboola Connection - Dynamics 365 Business Central Extractor" (ID: `dfb5f7c5-c2bc-4e68-b779-ffd2365528f0`).
2. After registering the application, review and grant the required API permissions (scopes), then copy the application ID from your tenant.
3. In the Dynamics 365 Business Central admin center, go to **Microsoft Entra Apps** and authorize the registered application, granting consent as needed.
4. In Dynamics 365 Business Central, open **Microsoft Entra Application**, add the registered application, assign the permissions you need, and grant consent.
5. In Keboola, authorize the component under the **Authorization** section.


Configuration
=============
**Configuration Root:**
- **Tenant ID**: Found in the URL: `https://businesscentral.dynamics.com/<tenant_id>/Production`
- **Company ID**: GUID of the company to extract data from (use "Load companies" to view available options)
- **Environment**: Select the Business Central environment (Production, Sandbox, etc.)

**Configuration Rows:**

### Data Source Options
The component supports the following data selection options:
- **Endpoint**: Select from available API endpoints (customers, items, salesOrders, etc.)
- **Selected Columns**: Choose specific columns or extract all available columns
- **Filter Expression**: Apply OData filter expressions to limit results (e.g., `displayName eq 'John'`)
- **Incremental Field**: Specify a datetime field for incremental data extraction
- **Initial Since Value**: Value used for the initial load

### Data Destination Options
- **Table Name**: Name of the output table in Keboola Storage. If left empty, defaults to the endpoint name.
- **Load Type**: In Full Load mode, the destination table is overwritten on each run. In Incremental Load mode, data is upserted into the destination table based on the primary key.
- **Primary Key**: List of primary key columns for incremental loads. Defaults to "id" if not specified.

### Debug Options
- **Debug**: Enable verbose logging for troubleshooting

Output
======

Provides a list of tables, foreign keys, and schema.

Development
-----------

To customize the local data folder path, replace the `CUSTOM_FOLDER` placeholder with your desired path in the `docker-compose.yml` file:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    volumes:
      - ./:/code
      - ./CUSTOM_FOLDER:/data
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Clone this repository, initialize the workspace, and run the component using the following
commands:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
git clone https://github.com/keboola/component-ms-dynamics365-business-central.git component-microsoft-dynamics-365-business-central
cd component-microsoft-dynamics-365-business-central
docker-compose build
docker-compose run --rm dev
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Run the test suite and perform lint checks using this command:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
docker-compose run --rm test
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Integration
===========

For details about deployment and integration with Keboola, refer to the
[deployment section of the developer
documentation](https://developers.keboola.com/extend/component/deployment/).
